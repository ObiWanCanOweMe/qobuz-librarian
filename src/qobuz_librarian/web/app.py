"""FastAPI web application for Qobuz Librarian."""
import asyncio
import concurrent.futures
import hashlib
import html
import json
import shutil
import threading
import time
import tomllib
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import NoCredsError
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web.csrf import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    StripServerHeaderMiddleware,
)

# Held for the lifetime of the web process. Module-level so Python won't
# garbage-collect it (which would silently release the flock).
_RUN_LOCK_HANDLE = None
# Set when run_lock.acquire() fails at startup — the holder's PID, used by
# every destructive route to refuse new work. Read-only routes (dashboard,
# search, settings view) stay open so the user can still figure out what's
# going on.
_LOCK_BUSY_PID = None
# True when the web app has deliberately released the run-lock so the terminal
# (CLI) can use it — set by the Settings "Mode" toggle, or at startup when
# QL_CLI_ONLY is set. Distinct from _LOCK_BUSY_PID (another process holds the
# lock unexpectedly): in CLI mode the web holds no lock on purpose and pauses
# its own download/scan endpoints so the two can't race over /staging.
_CLI_MODE = False
# Tri-state result of the startup token probe. None until the probe runs
# (or if the network glitched); True if Qobuz accepted the saved token;
# False if Qobuz returned AuthLost. The dashboard banner only fires on the
# explicit False so a transient network blip doesn't nag the user.
_TOKEN_VALID: bool | None = None


def _ql_notice_html(kind: str, body: str) -> str:
    return (
        f'<div class="ql-notice ql-notice-{kind}" '
        f'data-flash data-flash-kind="{kind}">{body}</div>'
    )


def _lock_busy_response(request):
    """Return a 503 response if the run-lock is busy OR a critical volume
    was unwritable at startup, else None."""
    if _CLI_MODE:
        msg = ("Terminal (CLI) mode is on, so downloads and scans are paused "
               "here. Resume on Settings → Mode (Resume web app).")
    elif _LOCK_BUSY_PID is not None:
        msg = ("Another Qobuz Librarian run is active. Downloads and scans are "
               "paused so only one process writes to the library at a time. "
               "Stop the other run first, then restart Qobuz Librarian.")
    elif _UNWRITABLE_VOLUMES:
        msg = (f"Required volume(s) not writable: "
               f"{', '.join(_UNWRITABLE_VOLUMES)}. On a NAS, set "
               "PUID/PGID to the share owner and confirm the host "
               "directories exist. Downloads can't run until fixed.")
    else:
        return None
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html("error", html.escape(msg)),
            status_code=200)
    return _tr(request, "lock_busy.html", {"msg": msg}, status_code=503)


# Populated at startup. Empty list means OK; non-empty means destructive
# POSTs return 503 until the container restarts with the volumes mounted.
_UNWRITABLE_VOLUMES: list[str] = []


def _resume_album_download(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_albums(j, chosen, _get_token())


def _resume_upgrade(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token())


def _resume_repair(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_repairs(j, chosen, _get_token())


def _resume_migration(job, args):
    from qobuz_librarian.web import flows
    dest = args.get("dest", "")
    in_place = bool(args.get("in_place"))
    src = args.get("src")
    allow_low_space = bool(args.get("allow_low_space"))
    return lambda j, chosen: flows.execute_migration(
        j, chosen, dest, in_place=in_place,
        src=Path(src) if src else None, allow_low_space=allow_low_space)


def _resume_downsample(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_downsamples(
        j, chosen, token=_get_optional_token())


def _upgrade_available(creds_ok: bool | None = None) -> bool:
    if creds_ok is None:
        creds_ok = bool(_read_creds().get("auth_token"))
    return bool(getattr(cfg, "UPGRADE_SCAN_ENABLED", True) and creds_ok)


def _upgrade_unavailable_response():
    from qobuz_librarian.web import review_badges
    review_badges.clear_ready("upgrade")
    return RedirectResponse(url="/", status_code=303)


def _upgrade_state_summary():
    from qobuz_librarian.quality import upgrade_state

    state = upgrade_state.load()
    complete = bool(state.get("complete"))
    candidates = (
        _visible_saved_review_candidates("upgrade", state.get("candidates") or [])
        if complete else [])
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
        "candidates": candidates,
        "count": len(candidates),
        "updated": _format_age(updated_at) if updated_at else None,
    }


def _downsample_state_summary():
    from qobuz_librarian.library import downsample_state

    state = downsample_state.load()
    complete = bool(state.get("complete"))
    candidates = (
        _visible_saved_review_candidates("downsample", state.get("candidates") or [])
        if complete else [])
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
        "candidates": candidates,
        "count": len(candidates),
        "updated": _format_age(updated_at) if updated_at else None,
    }


def _visible_saved_review_candidates(surface, candidates):
    if surface == "upgrade":
        from qobuz_librarian.quality import upgrade_state
        return upgrade_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    if surface == "downsample":
        from qobuz_librarian.library import downsample_state
        return downsample_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    return list(candidates or [])


def _saved_review_row(surface, spec):
    if surface == "downsample":
        payload = spec.get("payload") or {}
        row_payload = {
            "album_dir": spec.get("album_dir") or payload.get("album_dir") or "",
            "est_saving": spec.get("est_saving") or payload.get("est_saving") or 0,
        }
    else:
        row_payload = spec.get("payload") or {}
    return {
        "title": spec.get("title") or "?",
        "artist": spec.get("artist") or "",
        "detail": spec.get("detail") or "",
        "payload": row_payload,
    }


def _saved_review_key(surface, spec):
    return json.dumps(
        _saved_review_row(surface, spec),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _saved_review_signature(surface, state):
    rows = []
    for spec in state.get("candidates") or []:
        rows.append(_saved_review_row(surface, spec))
    rows.sort(key=lambda row: json.dumps(
        row, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _saved_review_specs_from_job(surface, job):
    with job._lock:
        candidates = list(job.candidates)
    specs = []
    for c in candidates:
        payload = c.get("payload") or {}
        if surface == "downsample":
            specs.append({
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "album_dir": payload.get("album_dir") or "",
                "est_saving": payload.get("est_saving") or 0,
            })
        else:
            specs.append({
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "payload": payload,
            })
    return specs


def _existing_saved_review_job(surface, signature):
    review_jobs = [
        job for job in job_mgr.registry.awaiting_review()
        if job.execute_kind == surface
    ]
    for job in review_jobs:
        if (job.execute_kind == surface
                and getattr(job, "_saved_review_signature", None) == signature):
            current_signature = _saved_review_signature(
                surface, {"candidates": _saved_review_specs_from_job(surface, job)})
            if current_signature == signature:
                return job
    for job in review_jobs:
        current_signature = _saved_review_signature(
            surface, {"candidates": _saved_review_specs_from_job(surface, job)})
        if current_signature == signature:
            job._saved_review_signature = signature
            return job
    return None


_SAVED_REVIEW_TITLES = {
    "upgrade": "Upgrade candidates",
    "downsample": "Downsample candidates",
}
_SAVED_REVIEW_LOCK = threading.Lock()


def _stale_saved_review_job(surface):
    review_jobs = [
        job for job in job_mgr.registry.awaiting_review()
        if job.execute_kind == surface
    ]
    for job in reversed(review_jobs):
        if (getattr(job, "_saved_review_signature", None) is not None
                or job.title == _SAVED_REVIEW_TITLES.get(surface)):
            return job
    return None


def _candidate_from_saved_spec(surface, spec, *, cid, seq, selected):
    row = _saved_review_row(surface, spec)
    return {
        "cid": cid,
        "seq": seq,
        "kind": surface,
        "title": row["title"],
        "artist": row["artist"],
        "detail": row["detail"],
        "payload": row["payload"],
        "selected": bool(selected),
    }


def _sync_saved_review_job(job, surface, state, signature):
    """Bring an existing saved-state review job back in line after restore/hide.

    The job is the user's live review session, so preserve ticks for candidates
    that still exist and add restored saved candidates unticked.
    """
    desired = list(state.get("candidates") or [])
    with job._lock:
        existing_raw_by_key = {
            _saved_review_key(surface, c): c
            for c in job.candidates
        }
        next_seq = max(
            [int(c.get("seq", -1)) for c in job.candidates
             if str(c.get("seq", "")).lstrip("-").isdigit()] + [-1]
        ) + 1
        if job._cand_seq < next_seq:
            job._cand_seq = next_seq
        rebuilt = []
        for spec in desired:
            key = _saved_review_key(surface, spec)
            old = existing_raw_by_key.get(key)
            if old is not None and old.get("cid") is not None:
                cid = old["cid"]
                seq = old.get("seq")
                if not isinstance(seq, int):
                    seq = job._cand_seq
                    job._cand_seq += 1
                selected = bool(old.get("selected"))
            else:
                cid = f"c{job._cand_seq}"
                seq = job._cand_seq
                job._cand_seq += 1
                selected = False
            rebuilt.append(
                _candidate_from_saved_spec(
                    surface, spec, cid=cid, seq=seq, selected=selected)
            )
        job.candidates = rebuilt
        job._saved_review_signature = signature
        n = len(rebuilt)
        if surface == "downsample":
            job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        else:
            job.summary = (
                f"{n} upgrade candidate{'s' if n != 1 else ''} ready to review.")
    from qobuz_librarian.web import job_persistence
    job_persistence.persist(job)
    job.notify_review_changed()
    return job


def _sync_saved_review_before_approve(job):
    surface = job.execute_kind
    if surface not in _SAVED_REVIEW_TITLES:
        return job
    if (getattr(job, "_saved_review_signature", None) is None
            and job.title != _SAVED_REVIEW_TITLES.get(surface)):
        return job
    state = (
        _upgrade_state_summary()
        if surface == "upgrade"
        else _downsample_state_summary()
    )
    current = {
        "candidates": state["candidates"] if state.get("complete") else [],
    }
    signature = _saved_review_signature(surface, current)
    with _SAVED_REVIEW_LOCK:
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return job
        return _sync_saved_review_job(job, surface, current, signature)


def _review_job_from_upgrade_state(state):
    from qobuz_librarian.web import flows

    with _SAVED_REVIEW_LOCK:
        signature = _saved_review_signature("upgrade", state)
        existing = _existing_saved_review_job("upgrade", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("upgrade")
        if stale is not None:
            return _sync_saved_review_job(stale, "upgrade", state, signature)
        job = job_mgr.Job(title="Upgrade candidates")
        job.kind = "scan"
        job.execute_kind = "upgrade"
        job.review_verb = "Upgrade"
        job._saved_review_signature = signature
        job._execute_fn = lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token())
        for spec in state.get("candidates") or []:
            job.add_candidate(
                kind="upgrade",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} upgrade candidate{'s' if n != 1 else ''} ready to review."
        job_mgr.registry.add(job)
        return job


def _review_job_from_downsample_state(state):
    from qobuz_librarian.web import flows

    with _SAVED_REVIEW_LOCK:
        signature = _saved_review_signature("downsample", state)
        existing = _existing_saved_review_job("downsample", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("downsample")
        if stale is not None:
            return _sync_saved_review_job(stale, "downsample", state, signature)
        job = job_mgr.Job(title="Downsample candidates")
        job.kind = "scan"
        job.execute_kind = "downsample"
        job.review_verb = "Downsample"
        job._saved_review_signature = signature
        job._execute_fn = lambda j, chosen: flows.execute_downsamples(
            j, chosen, token=_get_optional_token())
        for spec in state.get("candidates") or []:
            job.add_candidate(
                kind="downsample",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload={
                    "album_dir": spec.get("album_dir") or "",
                    "est_saving": spec.get("est_saving") or 0,
                },
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        job_mgr.registry.add(job)
        return job


# Names the persisted ``execute_kind`` strings so jobs survive a restart
# even though their original execute closure is gone. Each factory is
# called lazily, when the user actually approves the reloaded job, so
# the rebound function reads the current token rather than baking in the
# (possibly-rotated) one from the prior session.
_RESUME_EXECUTE: dict = {
    "album":        _resume_album_download,
    "library":      _resume_album_download,
    "new_releases": _resume_album_download,
    "upgrade":      _resume_upgrade,
    "repair":       _resume_repair,
    "migration":    _resume_migration,
    "downsample":   _resume_downsample,
}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import logging
    import os
    import shutil
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE
    _log = logging.getLogger("qobuz_librarian")
    from qobuz_librarian.ui_cli.logging import attach_file_handler
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
    if web_auth.auth_disabled():
        _log.warning("[warn] WEB_AUTH=none — web UI is unauthenticated, do not "
                     "expose to an untrusted network")
    else:
        cred_status = web_auth.apply_env_credentials()
        if cred_status in ("applied", "applied_weak"):
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
            if cred_status == "applied_weak":
                _log.warning(
                    "WEB_AUTH_PASSWORD is shorter than %d characters — it's the "
                    "only thing gating the web UI; use a longer one.",
                    web_auth.MIN_PASSWORD_LEN)
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment — only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
        if not web_auth.credentials_configured():
            _log.warning(
                "No web login configured — the open /setup screen is reachable "
                "to whoever hits the port first, who would then own the admin "
                "account. Seed WEB_AUTH_USER / WEB_AUTH_PASSWORD (compose) to "
                "close this window, and complete setup promptly on a trusted "
                "network.")
    from qobuz_librarian import run_lock
    from qobuz_librarian.api.auth import sync_streamrip_creds_from_env
    from qobuz_librarian.web import settings_store
    settings_store.load()
    # If creds are provided via env vars, mirror them into the streamrip
    # config now so web-triggered downloads don't fail on streamrip's
    # interactive auth prompt (the env-var path doesn't otherwise reach
    # streamrip's own config file).
    if sync_streamrip_creds_from_env() is False:
        _log.warning("Couldn't write env credentials into the streamrip "
                     "config; web downloads may fail until creds are set "
                     "via the Settings page.")
    # Acquire the same run lock the CLI uses, so a `docker compose run ...
    # cli` invocation while the web is up can't corrupt /staging. If we
    # CAN'T acquire it, _LOCK_BUSY_PID is set and every destructive route
    # refuses with 503 — silently continuing without the lock would let
    # two writers race in /staging and corrupt both their downloads.
    if os.environ.get("QL_CLI_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        # Terminal-first deployment: don't take the lock, so `docker exec ...
        # qobuz-librarian` always works. The web UI still serves for browsing
        # and Settings; its download/scan endpoints stay paused until the user
        # resumes web mode (which lasts until the next restart).
        _CLI_MODE = True
        _LOCK_BUSY_PID = None
        _log.info("QL_CLI_ONLY set — starting in terminal (CLI) mode; the web "
                  "app holds no lock and download/scan endpoints are paused.")
    else:
        _CLI_MODE = False
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire()
            _LOCK_BUSY_PID = None
        except run_lock.LockBusy as busy:
            _LOCK_BUSY_PID = busy.pid
            _log.error(
                "STARTUP: another Qobuz Librarian run holds the lock (pid %s). "
                "Background task will retry acquisition every 30s; in the "
                "meantime download/scan endpoints will return 503.",
                busy.pid,
            )

    async def _retry_lock():
        global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID
        while _LOCK_BUSY_PID is not None:
            await asyncio.sleep(30)
            # The lock state can change during the sleep: set_mode('cli') hands
            # the lock to the terminal (sets _CLI_MODE, clears _LOCK_BUSY_PID).
            # Re-check before acquiring, or we'd take the lock back in CLI mode
            # and wedge both the web app and the CLI until restart.
            if _LOCK_BUSY_PID is None or _CLI_MODE:
                return
            try:
                _RUN_LOCK_HANDLE = run_lock.acquire()
                _LOCK_BUSY_PID = None
                _log.info("Lock acquired; web write endpoints now active.")
                return
            except run_lock.LockBusy as busy:
                _LOCK_BUSY_PID = busy.pid
    if _LOCK_BUSY_PID is not None:
        asyncio.create_task(_retry_lock())

    _UNWRITABLE_VOLUMES.clear()
    # Opt-in via env so tests / dev runs that don't have /staging /music
    # mounted don't trip on the gate. The bundled compose sets this to 1.
    if os.environ.get("QL_CHECK_VOLUMES") == "1":
        # The label (the container-internal mount name) is what the operator
        # checks in their compose.yaml; the resolved cfg path is what's
        # actually being tested. Showing both makes "/music" warnings useful
        # even when MUSIC_ROOT is customised away from the bundled default,
        # and is_dir() catches a /dev/null-shaped mistake the W_OK alone misses.
        for label, path in (("STAGING_DIR", cfg.STAGING_DIR),
                            ("MUSIC_ROOT", cfg.MUSIC_ROOT)):
            p = Path(path)
            unreachable = not p.exists()
            not_a_dir = p.exists() and not p.is_dir()
            unwritable = p.exists() and p.is_dir() and not os.access(str(p), os.W_OK)
            if unreachable or not_a_dir or unwritable:
                _UNWRITABLE_VOLUMES.append(
                    f"{label}={path!s}"
                    + (" (missing)" if unreachable
                       else " (not a directory)" if not_a_dir
                       else " (read-only)"))
        if _UNWRITABLE_VOLUMES:
            _log.error("STARTUP: critical volumes not usable: %s. Write "
                       "endpoints will return 503 until container restarts "
                       "with mounts fixed.", _UNWRITABLE_VOLUMES)
    # Housekeeping the CLI also runs on each invocation — must be done
    # here too, otherwise a web-only deployment never sweeps stale upgrade
    # backups or orphan lyric-state entries.
    try:
        from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
        n = cleanup_old_upgrade_backups()
        if n:
            _log.info("Cleaned up %d stale upgrade backup(s) at startup.", n)
    except Exception as e:
        _log.debug("upgrade-backup cleanup error at startup: %s", e)
    try:
        from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
        _prune_lyric_state_orphans()
    except Exception as e:
        _log.debug("lyric-state prune error at startup: %s", e)
    # Heavy, throttled maintenance: prune_missing() stats every cached file
    # (100k+ on a NAS library), so run it in the background instead of blocking
    # the app from serving its first request.
    async def _bg_prune_flac_cache():
        try:
            from qobuz_librarian.library import flac_cache
            n_pruned = await asyncio.get_running_loop().run_in_executor(
                None, flac_cache.prune_missing)
            if n_pruned:
                _log.info("Pruned %d stale tag-cache entries.", n_pruned)
        except Exception as e:
            _log.debug("flac-cache prune error: %s", e)
        try:
            from qobuz_librarian.library import repair_cache
            repair_cache.prune_expired()
        except Exception as e:
            _log.debug("repair-cache prune error: %s", e)
    asyncio.create_task(_bg_prune_flac_cache())
    job_mgr.start_worker()
    if not shutil.which("rip"):
        _log.warning("`rip` (streamrip) not found in PATH — downloads will fail")
    if not shutil.which("beet"):
        _log.warning("`beet` (beets) not found in PATH — imports will fail")
    if not shutil.which("flac"):
        _log.warning("`flac` not found — FLAC integrity checks fall back to a size heuristic")
    if not shutil.which("ffmpeg"):
        _log.warning("`ffmpeg` not found — hi-res downsampling disabled")
    # Reload jobs from the prior session so an AWAITING_REVIEW scan's
    # candidates survive a container restart and queued/running downloads
    # don't silently vanish (they're rebadged FAILED with a retry hint).
    # Runs AFTER start_worker() above — benign because the work queues are empty
    # until restore re-queues into them, and the registry is lock-guarded, so the
    # worker only idle-ticks until restore hands it something.
    try:
        job_mgr.restore_jobs(_RESUME_EXECUTE)
    except Exception as e:
        _log.warning("couldn't restore prior jobs: %s — starting fresh.", e)
    # Probe the saved token against Qobuz so a stale slot — non-empty but
    # not actually authenticated — surfaces in the dashboard banner rather
    # than failing the user's first search.
    asyncio.create_task(_probe_token())
    # Keep the dashboard banner honest after startup: any in-session 401 from
    # the API client flips _TOKEN_VALID to False here, so a token that expires
    # mid-session shows "saved token isn't authenticating" immediately instead
    # of leaving stale green until the user happens to retry the failed action.
    from qobuz_librarian.api.auth import register_auth_state_listener
    register_auth_state_listener(_on_auth_state)
    yield
    # Release the flock explicitly — assigning None alone relies on GC
    # closing the file, which may not happen if any caller still holds
    # a reference.
    if _RUN_LOCK_HANDLE is not None:
        try:
            _RUN_LOCK_HANDLE.close()
        except OSError:
            pass
        _RUN_LOCK_HANDLE = None


def _classify_token(token):
    """Ask Qobuz whether a token works.

    Returns "ok", "rejected" (Qobuz refused the token), or "unreachable"
    (couldn't tell — network down, timeout, or a Qobuz-side hiccup). A 401
    or a 400 means Qobuz parsed the request and turned the token away;
    everything else is treated as inconclusive so a transient blip doesn't
    look like a bad token.
    """
    from qobuz_librarian.api.auth import AuthLost, QobuzError, friendly_qobuz_error
    from qobuz_librarian.api.client import qobuz_get
    try:
        qobuz_get("album/search", {"query": "ok", "limit": 1}, token)
        return "ok"
    except AuthLost:
        return "rejected"
    except QobuzError as e:
        return "rejected" if friendly_qobuz_error(e).startswith("HTTP 400") \
            else "unreachable"
    except Exception:
        return "unreachable"


def _on_auth_state(valid: bool) -> None:
    """Listener registered with api.auth so a 401 mid-session flips the
    dashboard banner without waiting for the next page-load probe."""
    global _TOKEN_VALID
    _TOKEN_VALID = bool(valid)


def _qobuz_ready() -> bool:
    """True when Qobuz-dependent UI actions are worth offering."""
    return bool(_read_creds().get("auth_token")) and _TOKEN_VALID is not False


def _recent_empty_hint() -> str:
    if not _read_creds().get("auth_token"):
        return "Set up Qobuz before searching."
    return "Search above to find an artist, album, or track."


async def _probe_token():
    """One-shot startup check that the saved token still authenticates.

    Sets ``_TOKEN_VALID`` to True/False/None: None means the result is
    inconclusive (no token saved, or the probe couldn't reach Qobuz), so
    the dashboard treats it as "don't nag yet."
    """
    global _TOKEN_VALID
    creds = _read_creds()
    if not creds.get("auth_token"):
        return
    token = creds["auth_token"]
    from qobuz_librarian.api.client import call_within
    try:
        verdict = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                          _classify_token, token)),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        verdict = "unreachable"
    if verdict == "ok":
        _TOKEN_VALID = True
    elif verdict == "rejected":
        _TOKEN_VALID = False


app = FastAPI(title="Qobuz Librarian", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

# AuthMiddleware is added first so it ends up innermost — it runs after the
# CSRF middleware, which keeps CSRF validation on the login/setup POSTs and
# lets the redirects it returns pick up the security headers. (The CSRF cookie
# rides only HTML responses, so it's seeded by the login/setup page GET the
# redirect bounces to, not by the redirect itself.)
app.add_middleware(web_auth.AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StripServerHeaderMiddleware)

_here = Path(__file__).parent
templates = Jinja2Templates(directory=str(_here / "templates"))


def _app_version() -> str:
    """Prefer source-checkout metadata so stale editable installs don't lie."""
    for parent in _here.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                version = data.get("project", {}).get("version")
                if version:
                    return str(version)
            except (OSError, tomllib.TOMLDecodeError):
                break
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("qobuz-librarian")
    except Exception:
        # Only reached on a broken / non-installed run; "unknown" is honest,
        # a hardcoded number here just goes stale on the next bump.
        return "unknown"


try:
    _APP_VERSION = _app_version()
except Exception:
    _APP_VERSION = "unknown"
templates.env.globals["app_version"] = _APP_VERSION
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"
# Server epoch at render, so a live elapsed clock can tick from a client-side
# baseline instead of trusting the browser's wall clock against a server epoch.
templates.env.globals["now_ts"] = time.time


def _fmt_clock(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _fmt_elapsed(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


templates.env.globals["fmt_clock"] = _fmt_clock
templates.env.globals["fmt_elapsed"] = _fmt_elapsed
# Whether to show a Log out control — true only when auth is on and set up.
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the app icon at the well-known path so the browser's automatic
    /favicon.ico probe (allowlisted past auth in web/auth.py) doesn't 404. The
    HTML pages also carry a <link rel="icon">; this covers the bare probe."""
    return FileResponse(static_dir / "icon.png", media_type="image/png")


def _asset_version() -> str:
    """Cache-bust key derived from the CONTENT of the served assets, not the
    release version. It changes whenever app.js / app.css / sw.js change, so an
    edit between releases reaches a returning visitor (and the service worker)
    without a version bump — and an unchanged asset keeps its cache across one.
    The semantic app_version is for display only."""
    h = hashlib.sha256()
    for name in ("app.js", "dist/app.css", "sw.js"):
        try:
            h.update((static_dir / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12] or _APP_VERSION


_ASSET_VERSION = _asset_version()
templates.env.globals["asset_version"] = _ASSET_VERSION


# Bake the asset version into the worker so its cache name changes whenever the
# served assets change. The script bytes then differ, which is what makes the
# browser pick up the new worker and purge stale caches — a fixed cache name
# left returning visitors on old assets.
_SW_JS = (static_dir / "sw.js").read_text(encoding="utf-8").replace(
    "__APP_VERSION__", _ASSET_VERSION)


@app.get("/sw.js")
async def service_worker():
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/healthz")
async def healthz():
    """Cheap liveness probe for HEALTHCHECK / uptime monitors."""
    return JSONResponse({"ok": True})


@app.head("/healthz")
async def healthz_head():
    """Uptime monitors HEAD before GET — return a body-less 200 so they
    don't mark the service down on a 405."""
    return Response(status_code=200)


@app.head("/queue")
async def queue_head():
    return Response(status_code=200)


@app.head("/settings")
async def settings_head():
    return Response(status_code=200)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    if cookie and web_auth.verify_session(cookie):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": ""})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    ip = (request.client.host if request.client else "") or "unknown"
    # A request already carrying a valid session is provably the logged-in user,
    # not the brute-forcer the throttle exists to stop — exempt it so a remote
    # flood of failed logins for the admin username can't lock the real admin out.
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    has_session = bool(cookie) and web_auth.verify_session(cookie)
    if not has_session and not web_auth.check_login_rate_limit(ip, username):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Too many failed attempts. Wait an hour and try again."},
            status_code=429)
    # Offload the 600k-round PBKDF2 to a thread so one login attempt can't stall
    # the single-worker event loop (health, API and SSE all freeze during a KDF
    # that runs on the loop thread).
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, web_auth.verify_login, username.strip(), password)
    if not ok:
        web_auth.record_login_failure(ip, username)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Incorrect username or password."},
            status_code=401)
    web_auth.clear_login_failures(ip, username)
    resp = RedirectResponse(url="/", status_code=303)
    web_auth.set_session_cookie(resp, request)
    return resp


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    # Revoke the session server-side, not just the browser cookie — otherwise a
    # captured cookie value stays valid for its full 30-day lifetime.
    web_auth.revoke_session(request.cookies.get(web_auth.SESSION_COOKIE))
    web_auth.clear_session_cookie(resp)
    return resp


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="setup.html",
                                      context={"error": "", "username": ""})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), confirm: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    user = username.strip()
    if not user:
        err = "Pick a username."
    elif len(password) < web_auth.MIN_PASSWORD_LEN:
        err = f"Use a password of at least {web_auth.MIN_PASSWORD_LEN} characters."
    elif password != confirm:
        err = "The two passwords don't match."
    else:
        err = ""
    if err:
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": err, "username": user}, status_code=400)
    # First-run setup is unauthenticated by necessity (no creds exist yet), so
    # whoever reaches the open port first claims admin. Log the client IP so the
    # takeover window is at least auditable; the prevention is seeding
    # WEB_AUTH_USER/WEB_AUTH_PASSWORD (now plumbed through compose).
    _ip = (request.client.host if request.client else "") or "unknown"
    import logging as _logging
    _logging.getLogger("qobuz_librarian").warning(
        "First-run /setup creating admin account from %s (username=%r).",
        _ip, user)
    if not web_auth.set_credentials(user, password):
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": "Couldn't save the login: the data volume "
                              "isn't writable. Check PUID/PGID and volume "
                              "permissions.", "username": user},
            status_code=500)
    resp = RedirectResponse(url="/", status_code=303)
    web_auth.set_session_cookie(resp, request)
    return resp


def _tr(request, name, context, *, status_code=200):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely. A route that already
    fetched the active job list for its own template (`/queue`, the dashboard)
    can pass it as `pending` and the badge derives from that — no second
    `pending_and_running()` call on the same render.
    """
    if "pending_job_count" not in context or "queue_has_running" not in context:
        active = context.get("pending") or job_mgr.registry.pending_and_running()
        context.setdefault("pending_job_count", len(active))
        context.setdefault(
            "queue_has_running",
            any(j.status.value in ('running', 'scanning') for j in active),
        )
    context.setdefault("cli_mode", _CLI_MODE)
    # Error/utility renders (e.g. the 404 page) don't name a nav section; an
    # explicit empty page just leaves every nav link inactive instead of
    # relying on Jinja's undefined-is-falsey behaviour.
    context.setdefault("page", "")
    # Standing health the navbar surfaces on every page, not just the dashboard:
    # a rejected token (auth lost mid-session) and a lock held by another
    # instance both stop downloads, and a user on Search/Queue shouldn't only
    # find out when a job fails. Both are cheap module-level flags — no I/O.
    creds_ok = bool(_read_creds().get("auth_token"))
    context.setdefault("health_qobuz_missing", not creds_ok)
    context.setdefault("health_token_invalid", _TOKEN_VALID is False)
    context.setdefault("health_lock_busy", bool(_LOCK_BUSY_PID))
    context.setdefault("upgrade_available", _upgrade_available(creds_ok))
    from qobuz_librarian.web import review_badges
    if (context.get("page") in review_badges.SURFACES
            and (context.get("page") != "upgrade" or context["upgrade_available"])):
        review_badges.mark_seen(context["page"])
    badges = review_badges.snapshot()
    if not context["upgrade_available"]:
        badges = dict(badges)
        badges["upgrade"] = False
    context.setdefault("nav_review_badges", badges)
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


async def _initial_artist_search_html(request: Request, query: str) -> str:
    """Render artist-name search results for dashboard links with ?kind=artist&q=."""
    query = str(query or "").strip()[:200]
    if not query:
        return ""
    artist_results = []
    error = None
    try:
        from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import search_artists
        token = _get_token()
        loop = asyncio.get_running_loop()
        artist_raw = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(
                    cfg.WEB_FETCH_TIMEOUT,
                    search_artists,
                    query,
                    token,
                    limit=cfg.ARTIST_LOOKUP_LIMIT,
                ),
            ),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        for a in artist_raw:
            if not a.get("id"):
                continue
            img = a.get("image") or {}
            cover = ""
            if isinstance(img, dict):
                cover = img.get("small") or img.get("thumbnail") or ""
            albums_count = a.get("albums_count")
            if isinstance(albums_count, dict):
                albums_count = albums_count.get("total")
            artist_results.append({
                "id": a.get("id"),
                "name": a.get("name") or "?",
                "albums_count": albums_count,
                "cover": cover if str(cover).startswith(
                    "https://static.qobuz.com/") else "",
            })
    except (SystemExit, NoCredsError):
        error = "No Qobuz credentials set. Visit Settings."
    except AuthLost:
        error = "Token is expired or invalid. Update it in Settings."
    except QobuzUnavailable:
        error = ("Qobuz is temporarily unavailable (network or rate limit). "
                 "Try again shortly.")
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except QobuzError:
        error = "Search failed. Try again."
    except Exception:
        import logging
        logging.getLogger("qobuz_librarian").exception(
            "initial artist search failed for %r", query)
        error = "Search failed. Try again."
    return templates.env.get_template("_search_results.html").render(
        request=request,
        q=query,
        results=[],
        album_groups=[],
        artist_results=artist_results,
        selected_artist=None,
        error=error,
        kind="artist",
        creds_ok=bool(_read_creds().get("auth_token")),
        qobuz_ready=_qobuz_ready(),
        page="search",
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a styled page for a mistyped/stale URL instead of a bare
    ``{"detail": "Not Found"}``. API routes and every non-404 status keep the
    JSON shape callers expect."""
    if exc.status_code == 404 and not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 404,
            "title": "Page not found",
            "msg": "That page doesn't exist. The link may have moved or been "
                   "mistyped.",
        }, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """An uncaught route error renders the styled page for browser paths instead
    of FastAPI's bare JSON 500. API routes keep JSON. The detail is logged, never
    shown, since it can carry internals."""
    import logging
    logging.getLogger("qobuz_librarian").exception(
        "Unhandled error on %s", request.scope.get("path", "?"))
    if not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 500,
            "title": "Something went wrong",
            "msg": "An unexpected error happened on the server. Try again, or "
                   "check the container logs if it keeps happening.",
        }, status_code=500)
    return JSONResponse({"detail": "internal server error"}, status_code=500)


# Serialises the dedupe-check-then-submit in queue_download: the network
# get_album() await between the early check and the submit leaves a window where
# two requests for one album both pass the check and queue it twice.
_DOWNLOAD_SUBMIT_LOCK = threading.Lock()


def _find_job_touching_album(album_id: str, skip_single_track: bool = False):
    """Return a pending/running/awaiting-review job that already covers
    album_id, either as its direct subject or as one of its candidates.
    ``skip_single_track`` ignores one-track downloads, so a full-album download
    doesn't fold onto a job that only downloaded one track from the album."""
    for j in job_mgr.registry.pending_and_running():
        if skip_single_track and (getattr(j, "single", None) or {}).get("track_id"):
            continue
        if j.album_id == album_id:
            return j
        # Snapshot: a SCANNING job appends to candidates from the worker thread,
        # and iterating it live can raise "list changed size during iteration".
        for cand in list(j.candidates or []):
            payload = cand.get("payload") or {}
            if payload.get("album_id") == album_id:
                return j
            qa = (payload.get("candidate") or {}).get("qobuz_album") or {}
            if qa.get("id") == album_id:
                return j
    return None


def _duplicate_download_job(album_id: str, track_id: str = "",
                            as_new_edition: bool = False):
    """The already-active job a new /download should fold onto, or None to let it
    queue. Matched by intent, not album id alone: "get this edition too" is a
    deliberate extra copy and never folds; a single-track download folds only onto an
    identical one; a normal full-album download folds onto another full-album job
    (or a scan candidate the user is about to review), but not onto a one-track
    download from the same album."""
    if as_new_edition:
        return None
    if track_id:
        for j in job_mgr.registry.pending_and_running():
            s = getattr(j, "single", None) or {}
            if s.get("album_id") == album_id and s.get("track_id") == str(track_id):
                return j
        return None
    return _find_job_touching_album(album_id, skip_single_track=True)


def _staging_album_count() -> int:
    """Album folders left in staging by an interrupted import. The CLI warns
    about these at startup (`_check_staging_occupied`); the web has no such
    signal, so a crash mid-import leaves web-only users with no idea files are
    stranded. Only meaningful when nothing is actively writing — the caller
    suppresses the banner while a job is running."""
    try:
        return sum(1 for d in cfg.STAGING_DIR.iterdir() if d.is_dir())
    except OSError:
        return 0


@app.head("/")
async def dashboard_head():
    """Uptime monitors / curl -I hit HEAD before GET; serve a body-less 200
    so they don't get a 405 and mark the service down."""
    return Response(status_code=200)


# Reentrant so the auto-triggers (which hold it) can call the _start_* helpers
# below (which re-acquire it). It makes every "is one already queued? → submit"
# check-and-submit atomic across both the manual POST and the dashboard auto
# path, so a manual click landing alongside an auto-trigger can't stack two.
_auto_check_lock = threading.RLock()


def _existing_new_release_check():
    """An active or awaiting-review new-release check, or None — so a second one
    isn't stacked on top of one already queued or waiting for review."""
    for j in job_mgr.registry.pending_and_running():  # ACTIVE incl awaiting_review
        if getattr(j, "execute_kind", "") == "new_releases":
            return j
    return None


def _start_new_release_check():
    """Submit a whole-library new-release check and return the job (or the one
    already queued). Shared by the manual Library-page option and the automatic
    dashboard trigger."""
    with _auto_check_lock:
        # The run-lock may have been handed to the terminal mid-submit (this can
        # run in an executor for POST /library). Re-check under the lock so a
        # scan can't start right after set_mode('cli') released the lock and then
        # race the CLI over /staging.
        if _CLI_MODE:
            return None
        existing = _existing_new_release_check()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        job = job_mgr.Job(title="New-release check")
        job.execute_kind = "new_releases"
        job_mgr.submit_scan(
            job,
            lambda j: flows.scan_new_releases(j, _get_token()),
            lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        )
        return job


def _new_release_review():
    """The awaiting-review new-release check for the dashboard badge, if any."""
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") == "new_releases":
            return {"id": j.id, "count": len(j.candidates)}
    return None


def _maybe_auto_check_new_releases():
    """Quietly run the new-release check on dashboard load when it's due.

    Read-only — it only parks a review list, never downloads — so it's safe to
    fire from a GET. Skipped when the check is off, the token is missing or
    known-bad, the CLI holds the lock, another job is actively working, a
    new-release list is already awaiting review, or the interval hasn't elapsed.
    """
    if (cfg.NEW_RELEASE_CHECK_INTERVAL <= 0 or _CLI_MODE
            or _LOCK_BUSY_PID is not None or _UNWRITABLE_VOLUMES):
        return
    # Don't bother (or thrash) when there's no token, or one we already know
    # Qobuz is rejecting — it would just fail on the first call every load.
    if _TOKEN_VALID is False or not _read_creds().get("auth_token"):
        return
    from qobuz_librarian.library import new_releases
    # Only after a full library scan has established the baseline — otherwise the
    # check would crawl every artist just to record a starting point and surface
    # nothing. A completed library scan seeds it (flows.scan_library).
    if not new_releases.is_baseline_complete():
        return
    # And never ahead of an interrupted library scan waiting to resume: finishing
    # that takes priority (it's what the user's resume needs the scan lane for),
    # and a delta check can wait until the library is whole again.
    from qobuz_librarian.library import scan_checkpoint
    if scan_checkpoint.pending() is not None:
        return
    # Serialise the check-and-submit so two concurrent dashboard loads can't
    # both pass the gate and queue the check twice.
    with _auto_check_lock:
        active = job_mgr.registry.pending_and_running()
        working = any(j.status != job_mgr.JobStatus.AWAITING_REVIEW for j in active)
        pending_check = any(getattr(j, "execute_kind", "") == "new_releases"
                            for j in active)
        if working or pending_check:
            return
        last = new_releases.last_run()
        if last is not None and (time.time() - last) < cfg.NEW_RELEASE_CHECK_INTERVAL:
            return
        # Stamp the attempt before submitting: the scan only advances the stamp
        # on a clean finish, so without this a failed/cancelled run would re-fire
        # on every load.
        new_releases.touch_run()
        _start_new_release_check()


_ANY_TARGET = object()


def _scan_target(job) -> str:
    """The slice of the library a scan covers: a single artist (the per-artist
    routes set ``job.artist``) or "" for a whole-library sweep. Dedup compares on
    this so re-scanning one artist folds onto / supersedes only that artist's own
    in-flight scan or parked review — never a different artist's, and never the
    whole-library pass. Case/whitespace-folded so "Bonobo" re-scans "bonobo"."""
    return (getattr(job, "artist", "") or "").strip().casefold()


def _active_scan(*kinds, statuses=("pending", "scanning"), target=_ANY_TARGET):
    """A job of one of the given execute_kinds in one of ``statuses``, or None —
    used to fold a double-submitted pass onto the one already in flight instead
    of stacking duplicate work. Defaults to the scan phase: a scan keeps its
    execute_kind through the post-review download (which runs as ``running``),
    so matching only pending/scanning lets a deliberate re-scan still queue
    behind a batch that's downloading. Run-to-completion jobs with no review
    (lyrics) pass their own running phase instead. ``target`` restricts the match
    to one artist's scan (or the whole-library pass); the default matches any."""
    for j in job_mgr.registry.pending_and_running():
        if getattr(j, "execute_kind", "") in kinds and j.status.value in statuses:
            if target is _ANY_TARGET or _scan_target(j) == target:
                return j
    return None


def _queue_wait(job):
    """Describe what a PENDING job is waiting behind on its worker lane, so the
    UI can explain the wait instead of showing a bare "Queued". Scans share one
    worker and downloads another (see web/jobs.py), so a job only waits behind
    others in its OWN lane (job.kind: "scan" | "download"). ``position`` counts
    how many run before it (the one holding the worker + any earlier-queued).
    Returns {"ahead_title", "lane", "position"} or None when nothing's ahead —
    i.e. it's about to start, so there's nothing to explain."""
    if job.status != job_mgr.JobStatus.PENDING:
        return None
    holder = None
    ahead = 0
    for j in job_mgr.registry.all():
        if j.id == job.id or j.kind != job.kind:
            continue
        if j.status in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING):
            holder = j  # the job actually occupying this lane's worker right now
        elif (j.status == job_mgr.JobStatus.PENDING
              and (j.created_at or 0) < (job.created_at or 0)):
            ahead += 1
    if holder is None and ahead == 0:
        return None
    return {
        "ahead_title": holder.title if holder else "",
        "lane": job.kind,
        "position": ahead + (1 if holder else 0),
    }


def _repair_current_job():
    """The repair job that owns the /repair surface right now: the most recent
    repair job still pending / scanning / awaiting-review / running. None means
    the surface is idle (show the start-or-resume form). This is what lets
    /repair stay the single authoritative repair page across every phase instead
    of handing a parked review off to /jobs/{id} — and it's why a review is never
    hidden behind a "Start scan" button that would silently discard it."""
    states = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING)
    cur = None
    for j in job_mgr.registry.all():
        if getattr(j, "execute_kind", "") != "repair" or j.status not in states:
            continue
        if cur is None or (j.created_at or 0) >= (cur.created_at or 0):
            cur = j
    return cur


# Library follows the same single-surface rule as Repair: the scan, its live
# progress, and the parked Missing Albums / Gap Fill review all live on
# /library — never handed off to /jobs/{id} under the Queue nav.
_LIBRARY_SURFACE_KINDS = ("library", "new_releases")


def _library_current_job():
    """The scan that owns the /library surface right now (baseline or
    new-release check, still pending / scanning / awaiting-review / running),
    or None when the surface is idle and shows the launcher. A parked review
    outranks running work: after a tab-scoped download splits the review, the
    user stays on the tab still waiting for them while the download runs in
    the queue."""
    states = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING)
    cur = None
    for j in job_mgr.registry.all():
        if (getattr(j, "execute_kind", "") not in _LIBRARY_SURFACE_KINDS
                or j.status not in states):
            continue
        if cur is None:
            cur = j
            continue
        j_rev = j.status == job_mgr.JobStatus.AWAITING_REVIEW
        cur_rev = cur.status == job_mgr.JobStatus.AWAITING_REVIEW
        if (j_rev, (j.created_at or 0)) >= (cur_rev, (cur.created_at or 0)):
            cur = j
    return cur


async def _submit_scan_deduped_async(job, scan_fn, execute_fn, *kinds, **kw):
    """Run _submit_scan_deduped off the event loop.

    It takes _auto_check_lock, which dashboard executor threads can hold across
    small (possibly NAS-backed) reads, so the loop must not block on it — the
    same reason POST /library offloads its submit. Every async scan route goes
    through this instead of calling _submit_scan_deduped directly on the loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, **kw))


def _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, statuses=("pending", "scanning")):
    """Submit a scan only if one of ``kinds`` isn't already active, atomically.

    Checking _active_scan and submitting in one locked step closes the window
    where two near-simultaneous POSTs (a double-click, or the auto-trigger
    landing with a manual click) both pass the check and stack duplicate scans.
    Returns the job to redirect to — the new one, or the in-flight duplicate."""
    with _auto_check_lock:
        target = _scan_target(job)
        existing = _active_scan(*kinds, statuses=statuses, target=target)
        if existing is not None:
            return existing
        # A re-scan supersedes the same artist's stale parked review (or the
        # whole-library pass's) instead of stacking a second one: the fresh scan
        # re-derives that target's candidates, so the old awaiting-review result
        # is obsolete, and parked reviews never self-clear so without this they
        # pile up forever. Scoping to the same target is what stops one artist's
        # scan from throwing away a different artist's un-reviewed candidates.
        # A running download isn't touched (only pending/scanning gate above).
        for old in job_mgr.registry.awaiting_review():
            if getattr(old, "execute_kind", "") in kinds and _scan_target(old) == target:
                job_mgr.cancel_review(old)
        job_mgr.submit_scan(job, scan_fn, execute_fn)
        return job


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    return _active_scan("library")


def _library_scan_state():
    """Whether a whole-library scan has something valid to scan."""
    root = Path(cfg.MUSIC_ROOT)
    if not root.exists():
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"{root} does not exist. Choose the location that contains your artist folders."
            ),
        }
    if not root.is_dir():
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"{root} is not a folder. Choose the location that contains your artist folders."
            ),
        }
    from qobuz_librarian.library.scanner import list_library_artists
    artists = list_library_artists()
    if not artists:
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"No artist folders with audio were found in {root}. Choose the location that contains your artist folders."
            ),
        }
    return {"ready": True, "count": len(artists), "message": ""}


def _start_library_scan(partial_only=False, force_full=False):
    """Submit a library scan and return the job. Shared by the Library page and
    the automatic first-run/resume trigger. scan_library resumes from a matching
    checkpoint on its own, so this is the same call whether starting or resuming.

    Deduped under the lock: if a library scan is already crawling, return it
    instead of stacking a second one (the manual button and the auto trigger can
    both land here at once)."""
    with _auto_check_lock:
        # Re-check the CLI handoff under the lock (see _start_new_release_check).
        if _CLI_MODE:
            return None
        existing = _active_library_scan()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        title = "Gap Fill scan" if partial_only else "Library scan"
        job = job_mgr.Job(title=title)
        job.execute_kind = "library"
        job_mgr.submit_scan(
            job,
            lambda j: flows.scan_library(
                j,
                _get_token(),
                partial_only=partial_only,
                force_full=force_full,
            ),
            lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        )
        return job


def _maybe_resume_library_scan():
    """Resume an interrupted library scan when the app is idle, driving it to
    completion across restarts.

    A FRESH first scan is NOT auto-started — the dashboard offers it as a choice
    (see ``offer_baseline``) so a brand-new user isn't hit with a long,
    network-heavy job unprompted. Once they start one and it gets interrupted, it
    leaves a checkpoint and resumes from here. Off entirely via AUTO_LIBRARY_SCAN.
    """
    if (not cfg.AUTO_LIBRARY_SCAN or _CLI_MODE or _LOCK_BUSY_PID is not None
            or _UNWRITABLE_VOLUMES):
        return
    if _TOKEN_VALID is False or not _read_creds().get("auth_token"):
        return
    from qobuz_librarian.library import new_releases, scan_checkpoint
    if new_releases.is_baseline_complete():
        return
    with _auto_check_lock:
        if any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running()):
            return  # something already working
        cp = scan_checkpoint.pending()
        if cp is not None:
            _start_library_scan(partial_only=(cp["kind"] == "partial"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", kind: str = "artist"):
    from qobuz_librarian import config as _cfg
    active_jobs = [j for j in job_mgr.registry.pending_and_running()
                   if j.status.value in ('running', 'scanning')]

    # These all read the (often NAS / network-mounted) data + music volumes —
    # the fetch log, the creds file, the lyric-retry file, and a staging
    # iterdir(). Run them off the event loop so a sleepy/flaky mount can't stall
    # every other request (health checks, SSE setup, search) while it blocks.
    def _gather_disk_state():
        from qobuz_librarian.integrations.lyrics import load_lyric_retry
        from qobuz_librarian.library import new_releases, scan_checkpoint
        from qobuz_librarian.ui_cli.prompts import _read_fetch_log
        # These read state files and may submit a background job, so they run
        # here (off the event loop) alongside the other disk work. Resume an
        # interrupted scan first (the new-release check is gated on the baseline).
        _maybe_resume_library_scan()
        _maybe_auto_check_new_releases()
        library_scan_state = _library_scan_state()
        return {
            "new_release_review": _new_release_review(),
            # First run offers the baseline scan as a Run/Skip choice rather than
            # auto-starting it; suppress the offer once the user skips it (the
            # dismiss marker) or turns it off via AUTO_LIBRARY_SCAN.
            "offer_baseline": (cfg.AUTO_LIBRARY_SCAN
                               and not new_releases.auto_scan_attempted()),
            # First-run setup banner: shown until a full library scan has seeded
            # the new-release baseline. setup_scanning = a library scan is now
            # pending/running — re-queried here (not from the pre-trigger
            # active_jobs) so the scan the auto-trigger just submitted shows.
            "baseline_complete": new_releases.is_baseline_complete(),
            "setup_scanning": _active_library_scan() is not None,
            "scan_resumable": scan_checkpoint.pending() is not None,
            "library_scan_state": library_scan_state,
            # An interrupted gap-scan, surfaced on the dashboard the way /library
            # already does — gated on no scan running. The setup banner above
            # covers the not-yet-baselined case, so index.html shows THIS only
            # once a baseline exists; otherwise the two would double up.
            "library_resume": (lambda cp: cp if cp is not None
                               and _active_library_scan() is None else None)(
                                   scan_checkpoint.pending()),
            # tail-only read so a long-running install with a multi-MB fetch log
            # doesn't slurp the whole file on every dashboard load. Hide the
            # no-op results (already complete / already best / skipped / no
            # change) so this lists actual downloads, not scans that found
            # nothing to fetch.
            "recent": list(reversed([
                e for e in _read_fetch_log(limit_tail=60)
                if e.get("result") not in {
                    "already_complete", "skipped_already_higher_quality",
                    "skipped_has_extras", "nothing_landed",
                }
            ]))[:8],
            # First-run nudge: a fresh install has no creds, so every search/scan
            # would fail cryptically — surface it up front. Filesystem-only.
            "creds_ok": bool(_read_creds().get("auth_token")),
            "qobuz_ready": _qobuz_ready(),
            "recent_empty_hint": _recent_empty_hint(),
            "lyric_retry_count":
                len(load_lyric_retry()) if _cfg.LYRIC_RETRY_FILE.exists() else 0,
            "staging_album_count": 0 if active_jobs else _staging_album_count(),
            "last_library_scan": _last_scan_age(),
            "last_new_release_check": _last_new_release_check_age(),
        }

    loop = asyncio.get_running_loop()
    disk = await loop.run_in_executor(None, _gather_disk_state)
    search_kind = str(kind or "").strip().lower()
    if search_kind not in ("artist", "album", "track"):
        search_kind = "artist"
    search_q = str(q or "").strip()[:200]
    initial_search_results = ""
    if search_kind == "artist" and search_q:
        initial_search_results = await _initial_artist_search_html(request, search_q)
    return _tr(request, "index.html", {
        "active_jobs": active_jobs,
        "pending": job_mgr.registry.pending_and_running(),
        "review": job_mgr.registry.awaiting_review(),
        "creds_token_valid": _TOKEN_VALID,
        "lock_busy_pid": _LOCK_BUSY_PID,
        "search_q": search_q,
        "search_kind": search_kind,
        "initial_search_results": initial_search_results,
        "page": "dashboard",
        **disk,
    })


@app.post("/lyric-retry")
async def lyric_retry(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    # A retry and a full backfill share the one lyric-state file, so they must
    # never run at once — fold onto whichever lyrics pass is already in flight.
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyric retry")
    job.execute_kind = "lyrics"
    job_mgr.submit(job, lambda j: flows.run_lyric_retry(j))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/search")
async def search_page():
    # Search lives on the dashboard now (it leads with the search bar), so this
    # path redirects to the current search surface instead of serving a second
    # page with no nav home.
    return RedirectResponse(url="/", status_code=307)


def _qobuz_quality_bits_rate(primary: dict | None,
                             fallback: dict | None = None) -> tuple[int, int]:
    """Return Qobuz source quality as (bits, sample_rate_hz)."""
    primary = primary or {}
    fallback = fallback or {}
    bits = primary.get("maximum_bit_depth") or fallback.get("maximum_bit_depth") or 0
    rate = (primary.get("maximum_sampling_rate")
            or fallback.get("maximum_sampling_rate") or 0)
    try:
        bits_i = int(bits)
    except (TypeError, ValueError):
        bits_i = 0
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        rate_f = 0.0
    if 0 < rate_f < 1000:
        rate_f *= 1000
    return bits_i, int(round(rate_f))


def _qobuz_quality_short_label(primary: dict | None,
                               fallback: dict | None = None) -> str:
    bits, rate = _qobuz_quality_bits_rate(primary, fallback)
    if not bits or not rate:
        return ""
    from qobuz_librarian.quality.tiers import format_quality
    return format_quality(bits, rate)


@app.post("/search", response_class=HTMLResponse)
async def do_search(request: Request, q: str = Form("", max_length=500),
                    kind: str = Form("album"),
                    artist_id: str = Form(""),
                    artist_name: str = Form("")):
    results = []
    album_groups = []
    artist_results = []
    selected_artist = None
    error = None
    query = q.strip()
    kind_raw = str(kind).strip().lower()
    kind = kind_raw if kind_raw in ("artist", "track") else "album"
    artist_id = str(artist_id or "").strip()
    artist_name = str(artist_name or "").strip()
    if not _is_htmx(request):
        return RedirectResponse(url="/", status_code=303)
    if query:
        # Imported before the try so the except clauses below can always name
        # them, even if a failure happens before the request reaches the API.
        from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
        try:
            token = _get_token()
            from qobuz_librarian.api.search import (
                get_album,
                get_artist_albums,
                get_track,
                search_albums,
                search_artists,
                search_tracks,
            )
            from qobuz_librarian.cli import parse_qobuz_url
            from qobuz_librarian.library.catalog import (
                album_year,
                find_album_dir_filesystem,
            )

            # If the user pasted a Qobuz URL, the placeholder says we
            # handle it — actually do so by fetching the album directly
            # instead of doing a text search on the URL string.
            try:
                _split = urllib.parse.urlsplit(query)
                netloc = _split.netloc.lower()
                is_qobuz_url = (_split.scheme in ("http", "https")
                                and (netloc == "qobuz.com"
                                     or netloc.endswith(".qobuz.com")))
            except ValueError:
                is_qobuz_url = False
            parsed = parse_qobuz_url(query) if is_qobuz_url else None
            raw = []
            loop = asyncio.get_running_loop()
            from qobuz_librarian.api.client import call_within
            if parsed and parsed[0] == "album" and kind == "track":
                # An album URL only resolves in Album mode; in Track mode it
                # would fetch the album and then be dropped as not-a-track,
                # leaving a blank "No results". Point the user at the toggle.
                error = ("That's an album URL. Switch to Album to download it, "
                         "or paste a single track to download one track.")
            elif parsed and parsed[0] == "album" and kind == "artist":
                error = "That's an album URL. Switch to Album to download it."
            elif parsed and parsed[0] == "album":
                try:
                    raw = [await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT, get_album, parsed[1], token)
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )]
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that album. Check the URL."
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "album fetch failed for %r", query)
                    error = "Couldn't fetch that album. Check the URL."
            elif parsed and parsed[0] == "track" and kind == "track":
                # Tracks mode: resolve the pasted track URL to that one track;
                # the track-results loop below renders it for a one-track download.
                try:
                    _t = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: call_within(
                            cfg.WEB_FETCH_TIMEOUT, get_track, parsed[1], token)),
                        timeout=cfg.WEB_FETCH_TIMEOUT)
                    raw = [_t] if _t else []
                    if not raw:
                        error = "Couldn't fetch that track. Check the URL."
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that track. Check the URL."
            elif parsed and parsed[0] == "track":
                # Album mode: a track URL -- point the user at the Track toggle
                # instead of the old (now false) "works on albums" message.
                error = ("That's a track URL. Switch to Track to download one "
                         "track, or paste the album URL in Album mode.")
            elif parsed:
                # Parsed as some other Qobuz URL kind (artist/playlist).
                if kind == "artist":
                    error = "Search artists by name. Paste album or track URLs only."
                else:
                    error = ("Only Qobuz album and track URLs are supported. "
                             "Search for an artist by name instead.")
            elif is_qobuz_url:
                # URL looks like qobuz.com but isn't a recognised format
                # (e.g. artist/interpreter or playlist page). Text-searching
                # the URL string returns nothing and confuses the user.
                if kind == "artist":
                    error = "Search artists by name. Paste album or track URLs only."
                else:
                    error = ("Only Qobuz album and track URLs are supported. "
                             "Search for an artist by name instead.")
            elif kind == "artist" and artist_id:
                try:
                    raw, artist_total = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                get_artist_albums,
                                artist_id,
                                token,
                                limit=cfg.ARTIST_CATALOG_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                    selected_artist = {
                        "id": artist_id,
                        "name": artist_name or query,
                        "total": artist_total,
                        "shown": len(raw),
                    }
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
            elif kind == "artist":
                try:
                    artist_raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                search_artists,
                                query,
                                token,
                                limit=cfg.ARTIST_LOOKUP_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                    for a in artist_raw:
                        if not a.get("id"):
                            continue
                        img = a.get("image") or {}
                        cover = ""
                        if isinstance(img, dict):
                            cover = img.get("small") or img.get("thumbnail") or ""
                        albums_count = a.get("albums_count")
                        if isinstance(albums_count, dict):
                            albums_count = albums_count.get("total")
                        artist_results.append({
                            "id": a.get("id"),
                            "name": a.get("name") or "?",
                            "albums_count": albums_count,
                            "cover": cover if str(cover).startswith(
                                "https://static.qobuz.com/") else "",
                        })
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
            else:
                _search_fn = search_tracks if kind == "track" else search_albums
                try:
                    raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(cfg.WEB_FETCH_TIMEOUT, _search_fn,
                                                query, token, limit=cfg.SEARCH_LIMIT),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."

            for t in (raw if kind == "track" else []):
                alb = t.get("album") or {}
                if not t.get("id") or not alb.get("id"):
                    continue
                _tbd, _tsr = _qobuz_quality_bits_rate(t, alb)
                _timg = alb.get("image") or {}
                _tcover = _timg.get("small") or _timg.get("thumbnail") or ""
                _perf = (t.get("performer") or {}).get("name")
                results.append({
                    "track_id":    t.get("id"),
                    "album_id":    alb.get("id"),
                    "title":       t.get("title") or "?",
                    "version":     t.get("version") or "",
                    "artist":      (alb.get("artist") or {}).get("name") or _perf or "?",
                    "album_title": alb.get("title") or "?",
                    "year":        album_year(alb) or "?",
                    "track_n":     t.get("track_number") or "?",
                    "total":       alb.get("tracks_count") or "?",
                    "quality":     _qobuz_quality_short_label(t, alb),
                    "hires":       _tbd >= 24,
                    "lossy":       _tbd == 0,
                    "bit_depth":   _tbd,
                    "sample_rate": _tsr,
                    "cover":       _tcover if _tcover.startswith(
                        "https://static.qobuz.com/") else "",
                })
            _album_raws = []
            for a in (raw if kind == "album" or selected_artist else []):
                if not a.get("id"):
                    continue
                _bd, _sr = _qobuz_quality_bits_rate(a)
                _img = a.get("image") or {}
                _cover = _img.get("small") or _img.get("thumbnail") or ""
                _qual = _qobuz_quality_short_label(a)
                results.append({
                    "id":      a.get("id"),
                    "title":   a.get("title") or "?",
                    "artist":  (a.get("artist") or {}).get("name") or "?",
                    "year":    album_year(a) or "?",
                    "tracks":  a.get("tracks_count") or "?",
                    "quality": _qual,
                    "hires":   _bd >= 24,
                    "lossy":   _bd == 0,
                    "bit_depth": _bd,
                    "sample_rate": _sr,
                    "cover":   _cover if _cover.startswith(
                        "https://static.qobuz.com/") else "",
                    "owned":   False,
                })
                _album_raws.append(a)

            # Flag results already in the library so search never offers a plain
            # Download on an album you own — the app is gap-fill, so that would
            # contradict its own purpose. Reuse the scans' owned-title match,
            # resolving each artist's folder once, off the event loop.
            if _album_raws:
                def _annotate_owned():
                    # Mark owned with the SAME filesystem resolver the download
                    # and scan paths use, so the badge agrees with them.
                    # find_album_dir_filesystem expands artist variants — incl.
                    # the comma-split for collaboration folders ("John Lennon,
                    # Yoko Ono") and diacritic folds — so an album filed under a
                    # collab or otherwise-named folder is still recognised.
                    # For an owned album also note the on-disk year:
                    # the year picks the user's pressing as the row (so the rest
                    # show as "other versions") while Search stays out of
                    # upgrade decisions.
                    from qobuz_librarian.library.catalog import _dir_year
                    for res, alb in zip(results, _album_raws):
                        try:
                            folder = find_album_dir_filesystem(alb)
                            if folder is None:
                                continue
                            res["owned"] = True
                            res["disk_year"] = _dir_year(folder.name)
                        except Exception:
                            pass
                try:
                    # Local disk work, not a Qobuz call — give it a generous
                    # bound of its own (the single listing keeps the real cost
                    # ~1s) and LOG if it ever trips, so an empty In-library
                    # column leaves a trace instead of looking like the feature
                    # vanished. Ownership is a nicety; search still returns.
                    _own_timeout = 20
                    await asyncio.wait_for(
                        loop.run_in_executor(None, _annotate_owned),
                        timeout=_own_timeout)
                except asyncio.TimeoutError:
                    import logging
                    logging.getLogger("qobuz_librarian").warning(
                        "ownership annotation timed out (%ss) for %r — results "
                        "shown without In-library marks", _own_timeout, query)
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "ownership annotation failed for %r", query)

            # Collapse the flat result list into one row per album: a remaster,
            # deluxe, and box set of the same record group together with the
            # alternates tucked under the main row, instead of the same album
            # scattering down the page. The edition Qobuz ranked first is the
            # row; the rest become its "other versions".
            if _album_raws:
                from qobuz_librarian.library.discovery import _is_live_release
                from qobuz_librarian.library.tags import (
                    normalize,
                    strip_album_decorations,
                    strip_leading_article,
                )
                by_key = {}
                for res, alb in zip(results, _album_raws):
                    ver = alb.get("version") or ""
                    # A live/session take is a different recording, not an edition
                    # of the studio album — key it on its own so it stays its own
                    # row instead of hiding under the studio album's "other
                    # versions". (A remaster/deluxe shares the base title and does
                    # group, which is what we want.)
                    if _is_live_release(res["title"]) or _is_live_release(ver):
                        base = normalize(res["title"] + " " + ver)
                    else:
                        base = normalize(strip_leading_article(
                            strip_album_decorations(res["title"])))
                    key = (normalize(res["artist"]), base)
                    g = by_key.get(key)
                    if g is None:
                        g = dict(res, editions=[])
                        by_key[key] = g
                        album_groups.append(g)
                    g["owned"] = g["owned"] or res["owned"]
                    if res.get("disk_year"):
                        g["disk_year"] = res["disk_year"]
                    g["editions"].append({
                        "id": res["id"],
                        "version": (alb.get("version") or "").strip(),
                        "year": res["year"], "tracks": res["tracks"],
                        "quality": res["quality"], "hires": res["hires"],
                        "lossy": res["lossy"], "bit_depth": res["bit_depth"],
                        "sample_rate": res["sample_rate"],
                        "cover": res["cover"],
                    })
                for g in album_groups:
                    eds = g["editions"]
                    # Float the pressing you actually own to the top (matched by
                    # the on-disk year) so it's the row and the rest read as
                    # "other versions" instead of offering you your own copy.
                    if g["owned"] and g.get("disk_year"):
                        for i, e in enumerate(eds):
                            if str(e["year"]) == str(g["disk_year"]):
                                if i:
                                    eds.insert(0, eds.pop(i))
                                break
                    rep = eds[0]
                    for f in ("id", "year", "tracks", "quality",
                              "hires", "lossy", "bit_depth", "sample_rate",
                              "cover", "version"):
                        g[f] = rep[f]
                    g["others"] = eds[1:]
        except (SystemExit, NoCredsError):
            error = "No Qobuz credentials set. Visit Settings."
        except AuthLost:
            error = "Token is expired or invalid. Update it in Settings."
        except QobuzUnavailable:
            error = ("Qobuz is temporarily unavailable (network or rate "
                     "limit). Try again shortly.")
        except QobuzError:
            error = "Search failed. Try again."
        except Exception:
            import logging
            logging.getLogger("qobuz_librarian").exception(
                "search failed for %r", query)
            error = "Search failed. Try again."
    creds_ok = bool(_read_creds().get("auth_token"))
    ctx = {"q": query, "results": results, "album_groups": album_groups,
           "artist_results": artist_results, "selected_artist": selected_artist,
           "error": error, "kind": kind,
           "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "search"}
    if _is_htmx(request):
        return _tr(request, "_search_results.html", ctx)
    return RedirectResponse(url="/", status_code=303)


_DOWNLOAD_SUMMARY_LABELS = {
    "already_complete": "Album already complete. Nothing to download.",
    "skipped_already_higher_quality": "Skipped: the library already has higher quality.",
    "skipped_has_extras": "Skipped: the library copy includes extra tracks.",
    "upgrade_only_no_op": "Already at or above the target quality.",
    "dry_run": "Dry run. Nothing downloaded.",
    "user_skipped": "Skipped at confirmation.",
    "lossy_only": "Qobuz only had lossy versions. Nothing downloaded.",
    "no_tracks": "Qobuz returned no tracks for this album.",
    "cancelled": "Cancelled. The partial download was discarded.",
    "upgrade_aborted_backup_failed": "Upgrade aborted: couldn't back up the original.",
    "partial": "Re-download came back incomplete; kept your original.",
    "not_imported": "Downloaded, but the import didn't land. Library unchanged.",
}


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict.

    Picks a phrase per result kind for the documented non-success branches,
    or builds the "N tracks downloaded" tally for an actual rip. Returns
    "" if there's nothing useful to say (process_album returned None / {})."""
    from qobuz_librarian.ui_cli.errors import plural

    if not r:
        return ""
    kind = r.get("result")
    if kind in _DOWNLOAD_SUMMARY_LABELS:
        return _DOWNLOAD_SUMMARY_LABELS[kind]
    if not r.get("imported"):
        return ""
    n_ok = r.get("n_ok", 0)
    n_fail = r.get("n_fail", 0)
    n_lossy = r.get("n_lossy", 0)
    parts = [f"{plural(n_ok, 'track')} downloaded"]
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_lossy:
        parts.append(f"{n_lossy} lossy-dropped")
    if r.get("upgrade_unverified"):
        parts.append("upgrade couldn't be verified; original kept")
    elif r.get("auto_upgrade"):
        parts.append("auto-upgrade verified")
    return ", ".join(parts) + "."


def _make_download_run(album, token, *, treat_as_new=False):
    """Return the run(j) callable used by both queue_download and job_retry.

    treat_as_new downloads the album as a brand-new one even if a different
    edition is already owned — the "get this edition too" path.
    """
    def run(j):
        from qobuz_librarian.modes.process import process_album
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import (
            _refresh_after_local_album_change,
            build_args,
        )
        args = build_args()
        with job_mgr.staging_lock():
            r = process_album(album, args, allow_force=False,
                              already_confirmed=True, token=token,
                              treat_as_new=treat_as_new) or {}
        benign = {"already_complete", "skipped_already_higher_quality",
                  "skipped_has_extras", "dry_run", "user_skipped",
                  "lossy_only", "no_tracks", "cancelled"}
        if r.get("result") not in benign and not r.get("imported"):
            j.status = job_mgr.JobStatus.FAILED
            if r.get("n_fail"):
                j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
            elif r.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            else:
                j.error = ("No tracks were retrieved. Qobuz may be rate-limiting "
                           "you, or the release is unavailable. Try again shortly.")
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
        # Surface a one-line outcome here so the /jobs page tells the user what
        # happened without expanding the log.
        summary = _summarize_download_result(r)
        if summary:
            j.summary = summary
        # Claiming/completing the album the normal way graduates it out of the
        # "downloaded single" state, so the rest stops being suppressed in scans.
        if r.get("imported"):
            _refresh_after_local_album_change(
                album,
                r,
                fallback_artist=(album.get("artist") or {}).get("name"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # A parked library review may still offer this album — drop it
            # there so the stale review can't download it a second time.
            from qobuz_librarian.web.flows import prune_library_review_candidates
            prune_library_review_candidates(album)
            from qobuz_librarian.library import hidden as hidden_mod
            hidden_mod.unmark_single(
                (album.get("artist") or {}).get("name") or "?",
                album.get("title") or "?")
    return run


def _make_single_track_run(album, track, token):
    """Run a single-track download: download just ``track`` via the per-track
    queue path (the same isolation repair uses — never a whole-album rip)."""
    def run(j):
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.library.catalog import (
            album_year,
            compute_missing,
            find_existing_tracks,
        )
        from qobuz_librarian.queue.builder import _build_queue_item
        from qobuz_librarian.queue.executor import _execute_download_queue
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import (
            _refresh_after_local_album_change,
            build_args,
        )
        args = build_args()
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        existing, album_dir = find_existing_tracks(album)
        missing, _present = compute_missing(qobuz_tracks, existing)
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it — that just lands a beets
        # ".1.flac" duplicate beside the copy you have — and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}”. Nothing downloaded."
            return
        qi = _build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist} — {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
        )
        with job_mgr.staging_lock():
            _execute_download_queue([qi], args, token)
        if not (qi.get("n_ok", 0) > 0 and qi.get("imported", False)
                and qi.get("n_fail", 0) == 0):
            j.status = job_mgr.JobStatus.FAILED
            if qi.get("n_fail"):
                j.error = f"{plural(qi.get('n_fail', 1), 'track')} failed"
            elif qi.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            else:
                j.error = ("Couldn't retrieve the track. Qobuz may be rate-limiting "
                           "you, or it's unavailable. Try again shortly.")
            return
        # Only mark it a single when explicitly configured. By default, a track
        # grab is just a track grab; future library scans should still offer the
        # rest of the album if it remains incomplete.
        marked = bool(cfg.SUPPRESS_SINGLE_TRACK_GAPS and len(missing) > 1)
        if marked:
            hidden_mod.mark_single(artist, title, album_year(album), album.get("id"))
            j.summary = (f"Got “{t_title}”, filed under {artist} / {title}. "
                         "The rest of the album stays out of scans.")
        elif len(missing) > 1:
            hidden_mod.unmark_single(artist, title)
            j.summary = (f"Got “{t_title}”, filed under {artist} / {title}. "
                         "Future scans can still offer the rest of the album.")
        else:
            # This download completed the album — it's a normal full album now, so
            # clear any single mark an earlier partial download left behind.
            # Without this the stale mark keeps the artist out of bulk scans and
            # the new-release check even though nothing is partial any more.
            hidden_mod.unmark_single(artist, title)
            j.summary = (f"Got “{t_title}”; that completed {title}, so it's "
                         "filed as a full album.")
            # Complete means any parked Gap Fill candidate for it is stale.
            from qobuz_librarian.web.flows import prune_library_review_candidates
            prune_library_review_candidates(album)
        _refresh_after_local_album_change(
            album,
            {"dir": qi.get("_resolved_post_dir") or album_dir},
            fallback_artist=artist,
            token=token,
            args=args,
            upgrade=True,
            downsample=True,
        )
        # Record which track was added so /undo can cleanly reverse it.
        j.single = {
            "album_id": str(album.get("id") or ""),
            "track_id": str(track.get("id") or ""),
            "dir": qi.get("_resolved_post_dir") or (str(album_dir) if album_dir else ""),
            "isrc": track.get("isrc") or "",
            "track_no": track.get("track_number"),
            "disc_no": track.get("media_number") or 1,
            "title": t_title, "artist": artist, "album": title,
            "marked": marked, "new_folder": album_dir is None,
        }
    return run


@app.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         as_new_edition: str = Form(""),
                         track_id: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    track_id = track_id.strip()
    if not album_id:
        msg = "Missing album id."
        if _is_htmx(request):
            # 200, not 400: htmx only swaps 2xx/3xx responses, so a 400 fragment
            # is silently dropped and the user sees no feedback. The notice
            # styling carries the "this failed" meaning instead of the status.
            return HTMLResponse(_ql_notice_html("error", html.escape(msg)))
        return RedirectResponse(url="/queue?error=" + urllib.parse.quote(msg),
                                status_code=303)
    # "Get this edition too" — download a different edition of an album the user
    # already owns, as a separate album. Bypasses the owned-check and treats it
    # as brand-new so it lands in its own (year) folder beside the existing copy
    # rather than being skipped or replacing it.
    download_as_new_edition = str(as_new_edition).strip().lower() in (
        "1", "true", "yes", "on")
    # Refuse true duplicates — same album already active or pending — but only of
    # the SAME intent (see _duplicate_download_job). Includes scan-flow jobs
    # awaiting review that have the album as one of their candidates, so a
    # search-then-download for an album the user just approved in an artist scan
    # still folds. A deliberate new-edition copy or a single-track download is its own
    # request and isn't swallowed by an unrelated job for the same album.
    existing = _duplicate_download_job(album_id, track_id, download_as_new_edition)
    if existing:
        if _is_htmx(request):
            return HTMLResponse(
                _ql_notice_html(
                    "warning",
                    f'Already queued. <a href="/jobs/{existing.id}" '
                    f'class="ql-inline-link">view job</a>.',
                )
            )
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    try:
        token = _get_token()
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import get_album
        loop = asyncio.get_running_loop()
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(cfg.WEB_FETCH_TIMEOUT, get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        if not download_as_new_edition and not track_id:
            def _already_complete():
                from qobuz_librarian.library.catalog import (
                    compute_missing,
                    find_album_dir_filesystem,
                    find_existing_tracks,
                )
                try:
                    album_dir = find_album_dir_filesystem(album)
                except Exception:
                    return False
                if album_dir is None:
                    return False
                try:
                    # Already resolved above; pass it through so we don't repeat
                    # the cached-subdir scan + fuzzy fallback for the same album.
                    existing_tracks, _ = find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    existing_tracks = []
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                # Only count it complete when nothing's missing. A partial album
                # (some present, some missing) returns False so process_album can
                # gap-fill the missing tracks instead of forcing a full re-rip.
                return bool(existing_tracks and qobuz_tracks) and not (
                    compute_missing(qobuz_tracks, existing_tracks)[0])

            # Resolving the album folder walks the (often NAS-mounted) library,
            # so keep it off the event loop — otherwise a large library stalls
            # every other request while this one request blocks.
            if await loop.run_in_executor(None, _already_complete):
                msg = "This album is already in your library."
                if _is_htmx(request):
                    # Offer the deliberate second-edition path instead of a dead
                    # end: a remaster or a different mix can be kept alongside the
                    # owned copy (it imports into its own (year) folder). The form
                    # re-posts with as_new_edition so the owned-check is skipped.
                    # A quiet card with the action stacked below the text, so the
                    # button never gets squeezed into a wrapped column on a phone.
                    aid = html.escape(album_id)
                    return HTMLResponse(
                        f'<div class="ql-download-choice">'
                        f'<div class="ql-download-choice-copy">'
                        f'<p>{html.escape(msg)}</p>'
                        f'<span>A remaster or different mix downloads into its '
                        f'own folder, kept alongside the existing library copy; '
                        f'same-year editions may merge in your player.</span></div>'
                        f'<form hx-post="/download" hx-target="#download-toast" '
                        f'hx-swap="innerHTML">'
                        f'<input type="hidden" name="album_id" value="{aid}">'
                        f'<input type="hidden" name="as_new_edition" value="1">'
                        f'<button type="submit" class="ql-btn ql-btn-primary ql-btn-sm '
                        f'w-full sm:w-auto whitespace-nowrap">'
                        f'Download this edition anyway</button></form></div>')
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg),
                    status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        single_track = None
        if track_id:
            _tracks = (album.get("tracks") or {}).get("items") or []
            single_track = next(
                (t for t in _tracks if str(t.get("id")) == track_id), None)
            if single_track is None:
                msg = "That track isn't on this album."
                if _is_htmx(request):
                    # 200, not 400: htmx drops non-2xx/3xx fragments, so a 400
                    # here renders nothing. The notice conveys the failure.
                    return HTMLResponse(_ql_notice_html("error", html.escape(msg)))
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg), status_code=303)
        job = job_mgr.Job(
            title=(single_track.get("title") or title) if single_track else title,
            artist=artist, album_id=album_id)
        if single_track:
            # Flagging it now (before the run fills in the undo details) is what
            # tells the UI to hide Cancel on this job — a one-track download is done
            # before you could catch it.
            job.single = {"album_id": album_id, "track_id": str(track_id)}

        # Re-check under the lock right before submitting: closes the race with
        # a concurrent /download for the same album across the get_album await.
        with _DOWNLOAD_SUBMIT_LOCK:
            dup = _duplicate_download_job(album_id, track_id, download_as_new_edition)
            if dup:
                if _is_htmx(request):
                    return HTMLResponse(
                        _ql_notice_html(
                            "warning",
                            f'Already queued. <a href="/jobs/{dup.id}" '
                            f'class="ql-inline-link">view job</a>.',
                        )
                    )
                return RedirectResponse(url=f"/jobs/{dup.id}", status_code=303)
            # Re-check the run-lock right before submitting. The album fetch
            # above awaited, and set_mode could have handed the lock to the
            # terminal in that window — while this not-yet-registered job was
            # invisible to set_mode's active-job check, so it wouldn't have
            # refused the handoff. There's no await between here and submit, so
            # this read and the registry add are atomic on the event loop: once
            # the job is registered the handoff sees it and is refused instead.
            busy = _lock_busy_response(request)
            if busy is not None:
                return busy
            run_fn = (_make_single_track_run(album, single_track, token)
                      if single_track
                      else _make_download_run(
                          album, token, treat_as_new=download_as_new_edition))
            job_mgr.submit(job, run_fn)
        if _is_htmx(request):
            return _tr(request, "_job_queued.html", {"job": job})
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        msg = "No Qobuz credentials set. Visit Settings."
        if _is_htmx(request):
            return HTMLResponse(_ql_notice_html("error", html.escape(msg)))
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception as e:
        from qobuz_librarian.api.auth import (
            AuthLost,
            QobuzError,
            QobuzUnavailable,
            friendly_qobuz_error,
        )
        if isinstance(e, asyncio.TimeoutError):
            user_msg = "Timed out reaching the Qobuz API. Try again."
        elif isinstance(e, QobuzUnavailable):
            user_msg = ("Qobuz is temporarily unavailable (network or rate "
                        "limit). Try again shortly.")
        elif isinstance(e, AuthLost):
            user_msg = "Token is expired or invalid. Update it in Settings."
        elif isinstance(e, QobuzError):
            cleaned = friendly_qobuz_error(e)
            if cleaned.startswith("HTTP 404"):
                user_msg = ("No album with that id. Check the URL "
                            "or use Search.")
            else:
                user_msg = ("Couldn't reach the Qobuz API. "
                            "Check the container's network.")
        else:
            user_msg = "Couldn't queue download. Check your token and try again."
        if _is_htmx(request):
            return HTMLResponse(
                _ql_notice_html("error", html.escape(user_msg)))
        msg = urllib.parse.quote(user_msg, safe="")
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)


@app.get("/artist")
async def artist_page(request: Request, artist: str = ""):
    # The public UI no longer has a separate Artist jobs page. Preserve old
    # direct links by landing on dashboard search in Artist mode.
    prefill = "".join(c for c in artist if c not in "<>\x00").strip()[:200]
    params = {"kind": "artist"}
    if prefill:
        params["q"] = prefill
    return RedirectResponse(url="/?" + urllib.parse.urlencode(params),
                            status_code=303)


def _clean_artist_name(artist):
    """Strip + length-cap + reject control chars. Returns (name, error_redirect).

    error_redirect is None on success, or a RedirectResponse back to dashboard
    artist search. Used by the artist scan + the per-artist power routes so
    they all reject the same way."""
    name = (artist or "").strip()[:200]
    if not name:
        return None, RedirectResponse(
            url="/?kind=artist",
            status_code=303,
        )
    if any(c in name for c in ("<", ">", "\x00")):
        return None, RedirectResponse(
            url="/?kind=artist",
            status_code=303,
        )
    return name, None


@app.post("/artist")
async def artist_scan(request: Request, artist: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Artist scan", artist=name)
    job.execute_kind = "album"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        "album")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, page: int = 1):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import scan_checkpoint
    creds_ok = bool(_read_creds().get("auth_token"))
    from qobuz_librarian.library import new_releases
    notice_bits = []
    _skipped = request.query_params.get("skipped", "")
    if _skipped.isdigit() and int(_skipped):
        n = int(_skipped)
        notice_bits.append(
            f"{n} album{'s' if n != 1 else ''} already in your library — skipped.")
    if request.query_params.get("noselection"):
        notice_bits.append("Nothing else is selected on that tab."
                           if notice_bits else
                           "Nothing is selected on that tab yet.")
    elif request.query_params.get("approved"):
        notice_bits.append("Download started — it's running in the queue.")
    notice = " ".join(notice_bits)
    ctx = {
        "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "library",
        "library_scan_state": _library_scan_state(),
        "library_notice": notice,
        "error": request.query_params.get("error", ""),
        # Freshness line: when a full gap scan last completed, and whether one
        # ever has (the new-release baseline is only seeded by a clean finish).
        "last_full_scan": _last_scan_age(),
        "baseline_complete": new_releases.is_baseline_complete(),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_MISSING),
        "JobStatus": job_mgr.JobStatus,
    }
    # Single-surface rule (same as /repair): a scan in flight or a parked
    # review renders inline right here, so results never hide behind the
    # launcher and never live under the Queue nav.
    ljob = _library_current_job()
    ctx["library_job"] = ljob
    if ljob is not None:
        ctx["queue_wait"] = _queue_wait(ljob)
        ctx.update(_review_context(ljob, page))
        ctx["library_resume"] = None
    else:
        # Resume hint: only when an interrupted baseline checkpoint exists and
        # nothing is running above.
        cp = scan_checkpoint.pending()
        ctx["library_resume"] = cp if cp is not None else None
    return _tr(request, "library.html", ctx)


@app.post("/library")
async def library_scan(
    request: Request,
    mode: str = Form("missing_albums"),
    force_full: str = Form(""),
):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    mode_norm = (mode or "").strip().lower()
    # Run the submit off the event loop: it takes _auto_check_lock, which the
    # dashboard auto-triggers can hold across small data-volume reads, and the
    # loop shouldn't block on a (possibly NAS) mount — same reason the dashboard
    # does its disk work in an executor.
    loop = asyncio.get_running_loop()
    if mode_norm == "new_releases":
        # A new-release check compares the catalog against the baseline a completed
        # library scan builds; with no baseline there's nothing to compare against,
        # so it would crawl every artist, surface nothing, and (the old bug) flip
        # the baseline "done" — stranding an interrupted library scan's resume.
        # Refuse and point at a library scan instead of running that empty crawl.
        from qobuz_librarian.library import new_releases as _nr
        if not _nr.is_baseline_complete():
            msg = "Run a full library scan first."
            if _is_htmx(request):
                return HTMLResponse(
                    f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                    status_code=200)
            return RedirectResponse(
                url="/library?error=" + urllib.parse.quote(msg), status_code=303)
        # Same job the dashboard auto-check submits; its own execute_kind so the
        # review screen badges the new releases (left un-ticked) and labels the surface.
        job = await loop.run_in_executor(None, _start_new_release_check)
        if job is None:    # lock handed to the terminal during the submit
            return _lock_busy_response(request) or RedirectResponse(
                url="/settings?mode=cli", status_code=303)
        return RedirectResponse(url="/library", status_code=303)
    scan_state = _library_scan_state()
    if not scan_state["ready"]:
        msg = scan_state["message"]
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                status_code=200)
        return RedirectResponse(
            url="/library?error=" + urllib.parse.quote(msg), status_code=303)
    # "library" (not "album") so the review screen knows this is the paced triage
    # surface; both modes run the same album executor and resume from a matching
    # checkpoint if one's waiting (see _start_library_scan / scan_library).
    force_full_scan = str(force_full or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    job = await loop.run_in_executor(
        None,
        lambda: _start_library_scan(
            partial_only=(mode_norm == "partial_fill"),
            force_full=force_full_scan,
        ),
    )
    if job is None:
        return _lock_busy_response(request) or RedirectResponse(
            url="/settings?mode=cli", status_code=303)
    # Land back on /library — the scan is watched and reviewed right here.
    return RedirectResponse(url="/library", status_code=303)


@app.post("/library/skip-setup")
async def skip_baseline_setup(request: Request):
    """Dismiss the first-run baseline-scan offer on the dashboard. The scan stays
    available any time from the Library page; this just stops the dashboard from
    offering it on every load."""
    from qobuz_librarian.library import new_releases
    new_releases.note_auto_scan_attempted()
    return RedirectResponse(url="/", status_code=303)


def _hidden_view(request, scope, *, page, restore_action, back_url):
    from qobuz_librarian.library import hidden as hidden_mod
    return _tr(request, "hidden.html", {
        "page": page, "scope": scope, "back_url": back_url,
        "restore_action": restore_action,
        "groups": hidden_mod.hidden_by_artist(scope)})


async def _restore_hidden(request, scope, redirect):
    # Mutates the hidden store, so it honours the run-lock like every other
    # state-changing POST — a restore mustn't race a CLI run or another job.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    form = await request.form()
    artists = form.getlist("artist")[:10000]
    fingerprints = form.getlist("fingerprint")[:10000]
    if artists:
        hidden_mod.restore(scope, artists)
    if fingerprints:
        hidden_mod.restore_albums(scope, fingerprints)
    return RedirectResponse(url=redirect, status_code=303)


@app.get("/library/hidden", response_class=HTMLResponse)
async def library_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_MISSING, page="library",
                        restore_action="/library/hidden/restore", back_url="/library")


@app.post("/library/hidden/restore")
async def library_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_MISSING, "/library/hidden")


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    creds_ok = bool(_read_creds().get("auth_token"))
    if not _upgrade_available(creds_ok):
        return _upgrade_unavailable_response()
    state = _upgrade_state_summary()
    return _tr(request, "upgrade.html", {
        "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "upgrade",
        "upgrade_state": state,
        "last_run": _tool_last_run_age("library"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)})


@app.get("/upgrade/hidden", response_class=HTMLResponse)
async def upgrade_hidden(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_UPGRADE, page="upgrade",
                        restore_action="/upgrade/hidden/restore", back_url="/upgrade")


@app.post("/upgrade/hidden/restore")
async def upgrade_hidden_restore(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden")


@app.post("/upgrade/review")
async def upgrade_review(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    state = _upgrade_state_summary()
    if not state["complete"] or not state["candidates"]:
        return RedirectResponse(url="/upgrade", status_code=303)
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(None, lambda: _review_job_from_upgrade_state(state))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/upgrade")
async def upgrade_review_legacy(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    return await upgrade_review(request)


@app.post("/upgrade/artist")
async def upgrade_scan_artist():
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    return RedirectResponse(url="/upgrade", status_code=303)


@app.get("/downsample", response_class=HTMLResponse)
async def downsample_page(request: Request):
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
    from qobuz_librarian.library import hidden as hidden_mod
    state = _downsample_state_summary()
    return _tr(request, "downsample.html", {
        "page": "downsample",
        "have_downsample": HAVE_DOWNSAMPLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "downsample_state": state,
        "last_run": _tool_last_run_age("downsample"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_DOWNSAMPLE)})


@app.get("/downsample/hidden", response_class=HTMLResponse)
async def downsample_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_DOWNSAMPLE, page="downsample",
                        restore_action="/downsample/hidden/restore",
                        back_url="/downsample")


@app.post("/downsample/hidden/restore")
async def downsample_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_DOWNSAMPLE,
                                 "/downsample/hidden")


@app.post("/downsample/review")
async def downsample_review(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    state = _downsample_state_summary()
    if not state["complete"] or not state["candidates"]:
        return RedirectResponse(url="/downsample", status_code=303)
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(
        None, lambda: _review_job_from_downsample_state(state))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/downsample")
async def downsample_scan(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Downsample scan")
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"  # the action rewrites files, not a download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_downsamples(j),
        lambda j, chosen: flows.execute_downsamples(
            j, chosen, token=_get_optional_token()),
        "downsample")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/downsample/artist")
async def downsample_scan_artist(request: Request, artist: str = Form("")):
    return RedirectResponse(url="/downsample", status_code=303)


@app.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request, page: int = 1):
    from qobuz_librarian.library import scan_checkpoint
    creds_ok = bool(_read_creds().get("auth_token"))
    # /repair is the SINGLE authoritative repair surface. When a repair job is in
    # flight or has results parked for review, render its live body inline here
    # (scanning → review → repairing → done) instead of bouncing to /jobs/{id}.
    # Only a genuinely idle surface shows the start/resume form — so a parked
    # review is never hidden behind a "Start scan" button (clicking which would
    # silently discard those results via the rescan dedup).
    rjob = _repair_current_job()
    ctx = {"creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(),
           "page": "repair", "repair_job": rjob,
           "JobStatus": job_mgr.JobStatus}
    if rjob is not None:
        ctx["queue_wait"] = _queue_wait(rjob)
        ctx.update(_review_context(rjob, page))
    else:
        # Idle: offer a resume only for a genuinely interrupted sweep (a stale
        # checkpoint), not one left by a run that's still active above.
        cp = scan_checkpoint.load("repair")
        ctx["repair_resume"] = (
            {"done": len(cp["scanned"]), "found": len(cp["candidates"])}
            if cp is not None else None)
        ctx["last_run"] = _tool_last_run_age("repair")
    return _tr(request, "repair.html", ctx)


@app.post("/repair")
async def repair_scan(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.review_verb = "Repair"  # the action refills damaged tracks, not a download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_repairs(j, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
        "repair")
    # Land back on /repair so the sweep is watched live right here — its card
    # streams each flagged album inline (and explains the wait if it's queued
    # behind another scan). When the scan finishes, the card's SSE done-handler
    # forwards to the job page's flagged-album review.
    return RedirectResponse(url="/repair", status_code=303)


@app.post("/repair/artist")
async def repair_scan_artist(request: Request, artist: str = Form("")):
    """Scan one artist's albums for ISRC-verified truncated FLACs. No
    checkpoint here (the focused single-artist run is fast); the whole-library
    sweep keeps its checkpoint because it can run for hours."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Repair scan", artist=name)
    job.execute_kind = "repair"
    job.review_verb = "Repair"  # the action refills damaged tracks, not a download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_repairs_for_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
        "repair")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/repair/history", response_class=HTMLResponse)
async def repair_history(request: Request):
    """Show what Repair has refilled in place — so the user knows which albums
    to refresh on an offline-sync client that may still serve the old broken
    file. The log itself is append-only on disk (DATA_DIR); this is read-only."""
    from qobuz_librarian.repair_log import read_repair_log_entries
    # Walks lines on the data volume — offload to match the dashboard's pattern
    # and keep the event loop free if the file is sizable.
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(
        None, lambda: read_repair_log_entries(limit=500))
    return _tr(request, "repair_history.html",
               {"page": "repair", "entries": entries})


@app.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    from qobuz_librarian.integrations.lyric_fetch import AVAILABLE
    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    lyrics_format = (cfg.LYRICS_FORMAT or "embed").lower()
    lyrics_format_label = {
        "embed": "Embedded tags",
        "sidecar": ".lrc sidecar files",
        "both": "Embedded tags and .lrc files",
    }.get(lyrics_format, lyrics_format)
    return _tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": AVAILABLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "last_run": _tool_last_run_age("lyrics"),
        "lyrics_format": lyrics_format_label,
        "providers": providers,
    })


@app.post("/lyrics")
async def lyrics_scan(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics scan")
    job.execute_kind = "lyrics"
    job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/lyrics/artist")
async def lyrics_scan_artist(request: Request, artist: str = Form("")):
    """Fetch lyrics for one artist's library tracks only. Same state file as
    the whole-library run, so this still skips tracks an earlier run resolved."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics scan", artist=name)
    job.execute_kind = "lyrics"
    job_mgr.submit(
        job,
        lambda j: flows.run_lyrics_for_artist(
            j, name, rescan=rescan, synced_only=synced_only),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _migrate_checks(src, dest):
    import os

    from qobuz_librarian.library.migrate import _existing_ancestor
    checks = []
    for label, path in (("Source folder", src), ("Destination folder", dest)):
        if not path:
            checks.append({"label": label, "ok": False, "detail": "not set"})
            continue
        p = Path(path)
        is_dest = label.startswith("Destination")
        if not p.exists():
            # The migration creates the destination tree, so a not-yet-created
            # dest is fine as long as a writable ancestor exists to land it in.
            anc = _existing_ancestor(p) if is_dest else None
            if is_dest and anc and os.access(str(anc), os.W_OK):
                checks.append({"label": label, "ok": True,
                               "detail": f"{p} (will be created under {anc})"})
            elif is_dest:
                checks.append({"label": label, "ok": False,
                               "detail": f"{p} can't be created. Nearest existing "
                                         f"folder {anc or p.anchor} is not writable"})
            else:
                checks.append({"label": label, "ok": False, "detail": f"{p} does not exist"})
        elif not p.is_dir():
            checks.append({"label": label, "ok": False, "detail": f"{p} is not a directory"})
        elif not os.access(str(p), os.R_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not readable"})
        elif is_dest and not os.access(str(p), os.W_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not writable"})
        else:
            checks.append({"label": label, "ok": True, "detail": str(p)})
    return checks


@app.get("/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    return _tr(request, "migrate.html", {
        "page": "migrate",
        "src": src,
        "dest": dest,
        "configured": bool(src and dest),
        "migrate_checks": _migrate_checks(src, dest),
    })


@app.post("/migrate")
async def migrate_scan(request: Request):
    # No credential check: migration only reads and reorganises local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import migrate as engine
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    form = await request.form()
    use_acoustid = form.get("acoustid") == "on"
    in_place = form.get("in_place") == "on"
    allow_low_space = form.get("allow_low_space") == "on"
    if not src or not dest:
        err = ("Set MIGRATE_SRC and MIGRATE_DEST: the source library and "
               "the destination for the organised copy, then try again.")
    else:
        err = engine.validate_paths(Path(src), Path(dest), in_place=in_place)
    if err:
        return _tr(request, "migrate.html", {
            "page": "migrate", "src": src, "dest": dest,
            "configured": bool(src and dest), "error": err,
            "migrate_checks": _migrate_checks(src, dest)})
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Library migration")
    job.review_verb = "Move" if in_place else "Copy"
    job.execute_kind = "migration"
    # src is persisted so a resume after restart can still prune the emptied
    # source folders on an in-place move (the live execute below gets it too).
    job.execute_args = {"dest": str(dest), "in_place": bool(in_place),
                        "src": str(src), "allow_low_space": bool(allow_low_space)}
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_migration(j, src, dest, use_acoustid=use_acoustid,
                                       in_place=in_place),
        lambda j, chosen: flows.execute_migration(j, chosen, dest,
                                                  in_place=in_place, src=src,
                                                  allow_low_space=allow_low_space),
        "migration")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str, approved: bool = False,
                   stale: bool = False, noselection: bool = False, page: int = 1):
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(url="/queue", status_code=303)
        historical = True
    ctx = {"job": job, "page": "queue",
           "approved": approved, "stale": stale, "noselection": noselection,
           "historical": historical,
           "queue_wait": _queue_wait(job),
           "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page))
    return _tr(request, "job.html", ctx)


def _review_context(job, page=1, query="", tab=""):
    """Template vars for a paginated awaiting-review body: the current page's
    artist groups, the page number/count, and the authoritative whole-set
    counts. Cheap no-op for non-review states (no candidates → one empty page).

    A library review always splits into its two tabs — Missing Albums and Gap
    Fill — and ``tab`` picks one. With no explicit pick, land on Missing Albums
    unless it's empty and Gap Fill isn't. Other review kinds render untabbed.
    """
    from qobuz_librarian.ui_cli.colors import format_size
    tab_counts = None
    if job.execute_kind == "library":
        totals = _review_tab_totals(job)
        if totals["missing"] or totals["gaps"]:
            tab_counts = totals
    if tab_counts:
        if tab not in ("missing", "gaps"):
            tab = ("gaps" if tab_counts["gaps"] and not tab_counts["missing"]
                   else "missing")
    else:
        tab = ""
    groups = _review_artist_groups(job, query, tab)
    page_groups, page, n_pages = _paginate_groups(groups, page)
    counts = job.selection_counts()
    from qobuz_librarian.library import hidden as hidden_mod
    return {
        "review_groups": page_groups,
        "review_page": page,
        "review_pages": n_pages,
        "review_query": query,
        "review_tab": tab,
        "review_tab_counts": tab_counts,
        "review_hidden_count": hidden_mod.count(_hide_scope(job.execute_kind)),
        "review_counts": counts,
        "review_reclaimable_label": (format_size(counts["reclaimable"])
                                     if counts["reclaimable"] else ""),
        "review_page_size": REVIEW_PAGE_ARTISTS,
    }


@app.get("/jobs/{job_id}/content", response_class=HTMLResponse)
async def job_content(request: Request, job_id: str, page: int = 1):
    """The job page's state-specific body, on its own. The live page swaps
    this in when the SSE stream reports the job finished, so the terminal
    view has one render path — the server's — instead of a faked-up bar."""
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
        historical = True
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus,
           "historical": historical,
           "queue_wait": _queue_wait(job)}
    ctx.update(_review_context(job, page))
    return _tr(request, "_job_body.html", ctx)


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def job_review_page(request: Request, job_id: str, page: int = 1,
                          q: str = "", tab: str = ""):
    """One page of the paginated review list (groups + pager + summary), for
    Prev/Next, the whole-set artist filter, and a library review's tab switch.
    Rendered from saved selection flags, so ticks persist and span pages."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page, q, tab))
    return _tr(request, "_review_page.html", ctx)


def _split_off_inactive_tab(job, tab):
    """Before a tab-scoped approve: move the tab the user ISN'T looking at into
    its own parked review job, so downloading one tab never consumes the other
    tab's un-reviewed candidates or their saved ticks. Returns the parked job,
    or None when the inactive tab is empty (nothing to protect)."""
    from qobuz_librarian.web import flows, job_persistence
    gap_active = tab == "gaps"
    with job._lock:
        keep, split = [], []
        for c in job.candidates:
            (keep if flows.is_gap_candidate(c) == gap_active else split).append(c)
        if not split:
            return None
        job.candidates = keep
    other = job_mgr.Job(title=job.title, kind=job.kind,
                        execute_kind=job.execute_kind,
                        execute_args=dict(job.execute_args or {}),
                        review_verb=job.review_verb,
                        status=job_mgr.JobStatus.AWAITING_REVIEW)
    other.candidates = split  # cids, seqs, and saved ticks ride along
    factory = _RESUME_EXECUTE.get(other.execute_kind)
    if factory is not None:
        other._execute_fn = factory(other, other.execute_args)
    job_mgr.registry.add(other)
    job_persistence.persist(other)
    job_persistence.persist(job)
    job.notify_review_changed()
    return other


@app.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    # Repair and Library stay on their single surfaces through the executing
    # phase; every other kind keeps using the job page.
    if job.execute_kind == "repair":
        dest = "/repair"
    elif job.execute_kind in _LIBRARY_SURFACE_KINDS:
        dest = "/library"
    else:
        dest = f"/jobs/{job_id}"
    # Guard a zero-selection approve: with nothing ticked, approving would flip
    # the job to done over an empty set and flash success while quietly
    # discarding the whole review. The submit button is disabled client-side, but
    # a direct POST or a stale page can still reach here — keep it in review and
    # say so instead.
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        job = _sync_saved_review_before_approve(job)
    # A library review approves per tab: the button acts on the tab the user is
    # looking at, and only that tab. Zero-selection is judged within the tab,
    # and the other tab's candidates split into their own parked review before
    # the download consumes this job. No tab posted (older page, direct POST)
    # keeps the whole-review behavior.
    form = await request.form()
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    loop = asyncio.get_running_loop()
    skipped = 0
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind in _LIBRARY_SURFACE_KINDS):
        # The review may have gone stale while parked: an album grabbed from
        # Search (or added by hand) can still sit here as a missing-album
        # candidate. Re-check against the disk and drop what's already owned,
        # instead of downloading it again. Disk probes — off the event loop.
        from qobuz_librarian.web import flows
        skipped = await loop.run_in_executor(
            None, lambda: flows.drop_owned_missing_candidates(job))
        if skipped and job_mgr.finalize_review_if_empty(job):
            return RedirectResponse(url=f"{dest}?skipped={skipped}",
                                    status_code=303)
    _skip_q = f"&skipped={skipped}" if skipped else ""
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        from qobuz_librarian.web import flows
        if tab:
            gap_active = tab == "gaps"
            with job._lock:
                has_pick = any(
                    c.get("selected") for c in job.candidates
                    if flows.is_gap_candidate(c) == gap_active)
        else:
            has_pick = any(c.get("selected") for c in job.candidates)
        if not has_pick:
            return RedirectResponse(url=f"{dest}?noselection=1{_skip_q}",
                                    status_code=303)
    # Selection is saved server-side as the user ticks (the paginated review no
    # longer carries every checkbox in the form), so approve runs against the
    # saved flags — passing None keeps them as-is rather than reading the form.
    # Offload to a thread: approve() does a json.dumps of up to JOB_CANDIDATE_CAP
    # candidate dicts + a SQLite commit, which would block the single event loop
    # (freezing every SSE stream / other request) for a large parked review —
    # the same reason /select was offloaded.
    def _split_and_approve():
        if tab and job.status == job_mgr.JobStatus.AWAITING_REVIEW:
            _split_off_inactive_tab(job, tab)
        return job_mgr.approve(job, None)

    approved = await loop.run_in_executor(None, _split_and_approve)
    flag = "approved=1" if approved else "stale=1"
    return RedirectResponse(url=f"{dest}?{flag}{_skip_q}", status_code=303)


# Review kinds that get the paced-triage surface (unticked and hideable). They
# share one review screen; hidden-store scope decides where dismissals land.
_TRIAGE_KINDS = ("library", "upgrade", "new_releases", "downsample")

# Kinds whose review screen has server-backed per-candidate selection. Artist
# ("album") scans render the same checkboxes and approve from the saved flags,
# so their ticks must persist too — without "album" here every tick 404s and
# the user's edits never reach the server.
_SELECTABLE_KINDS = _TRIAGE_KINDS + ("repair", "migration", "album")


def _hide_scope(execute_kind):
    from qobuz_librarian.library import hidden as hidden_mod
    if execute_kind == "upgrade":
        return hidden_mod.SCOPE_UPGRADE
    if execute_kind == "downsample":
        return hidden_mod.SCOPE_DOWNSAMPLE
    return hidden_mod.SCOPE_MISSING


# Artist groups per review page. A huge gap scan can surface thousands of
# albums; rendering them all is what made the review page tank, so the server
# pages by whole artist groups (an album never splits across a page).
REVIEW_PAGE_ARTISTS = 40


def _artist_sort_key(name: str) -> str:
    """Order artists ignoring a leading article, so 'The Beatles' files under B
    (not T) and 'A Tribe Called Quest' under T — the way music libraries sort.
    Case-insensitive."""
    low = (name or "").strip().casefold()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            return low[len(art):]
    return low


def _review_artist_groups(job, query="", tab=""):
    """Candidates grouped by artist for the review screen, in a deterministic
    order so pagination is stable across reloads. ``query`` filters across the
    WHOLE set (artist name or any album title), so the filter spans pages, not
    just the one on screen. ``tab`` narrows a library review to one side of its
    Missing Albums / Gap Fill split. Returns a list of (artist, items) pairs."""
    from qobuz_librarian.web import flows
    with job._lock:
        cands = list(job.candidates)
    q = (query or "").strip().lower()
    groups: dict = {}
    for c in cands:
        if tab and flows.is_gap_candidate(c) != (tab == "gaps"):
            continue
        artist = c.get("artist") or ""
        if q:
            hay = artist + " " + (c.get("title") or "")
            if q not in hay.lower():
                continue
        groups.setdefault(artist, []).append(c)
    # Sort groups by music-library order, tracks by their stable seq.
    ordered = []
    for artist in sorted(groups, key=_artist_sort_key):
        items = sorted(groups[artist], key=lambda c: c.get("seq", 0))
        ordered.append((artist, items))
    return ordered


def _paginate_groups(groups, page):
    """Slice artist groups into one page. Returns (page_groups, page, n_pages).
    ``page`` is clamped into range so a stale/empty page lands somewhere valid."""
    n_pages = max(1, (len(groups) + REVIEW_PAGE_ARTISTS - 1) // REVIEW_PAGE_ARTISTS)
    page = max(1, min(int(page or 1), n_pages))
    start = (page - 1) * REVIEW_PAGE_ARTISTS
    return groups[start:start + REVIEW_PAGE_ARTISTS], page, n_pages


def _get_reviewable_job(job_id):
    """A job from the live registry, or rehydrated from disk if it has been
    evicted — so a restored/archived awaiting-review job's selection and pager
    keep working, not just the page render. Returns None if it's nowhere."""
    job = job_mgr.registry.get(job_id)
    if job is None:
        job = job_mgr.load_historical_job(job_id)
    return job


def _selection_payload(job):
    """JSON the selection/hide endpoints return so every open tab can refresh
    its counts from the server instead of recounting a partial DOM."""
    from qobuz_librarian.ui_cli.colors import format_size
    c = job.selection_counts()
    payload = {
        "selected": c["selected"],
        "total": c["total"],
        "artists": c["artists"],
        "reclaimable": c["reclaimable"],
        "reclaimable_label": format_size(c["reclaimable"]) if c["reclaimable"] else "",
    }
    if job.execute_kind == "library":
        totals = _review_tab_totals(job)
        payload["missing_total"] = totals["missing"]
        payload["gap_total"] = totals["gaps"]
        payload["missing_selected"] = totals["missing_selected"]
        payload["gap_selected"] = totals["gaps_selected"]
    return payload


def _review_tab_totals(job):
    """Whole-set totals and selected counts behind a library review's Missing
    Albums / Gap Fill tabs, ignoring the page filter so the tab labels stay
    truthful. Selected counts feed the tab-scoped bulk bar: what the user sees
    on the active tab is exactly what Download/Dismiss will act on."""
    from qobuz_librarian.web import flows
    gaps = gaps_sel = missing_sel = 0
    with job._lock:
        total = len(job.candidates)
        for c in job.candidates:
            if flows.is_gap_candidate(c):
                gaps += 1
                gaps_sel += 1 if c.get("selected") else 0
            elif c.get("selected"):
                missing_sel += 1
    return {"missing": total - gaps, "gaps": gaps,
            "missing_selected": missing_sel, "gaps_selected": gaps_sel}


@app.post("/jobs/{job_id}/select")
async def job_select(request: Request, job_id: str):
    """Persist a single tick/untick. The review page doesn't rely on the posted
    checkboxes (pagination means most aren't in the DOM), so each toggle saves
    immediately and the saved flags are the source of truth at download."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    from qobuz_librarian.web import job_persistence
    form = await request.form()
    cid = (form.get("cid") or "").strip()
    on = (form.get("checked") or "").strip().lower() in ("1", "true", "on", "yes")
    if cid and job.set_selected(cid, on):
        # persist() json.dumps the whole candidates list (multi-MB near the
        # candidate cap) and writes SQLite under a lock — keep it off the event
        # loop so a single checkbox tick doesn't stall every other request.
        await asyncio.get_running_loop().run_in_executor(
            None, job_persistence.persist, job)
        job.notify_review_changed()
    return JSONResponse(_selection_payload(job))


@app.post("/jobs/{job_id}/select-all")
async def job_select_all(request: Request, job_id: str):
    """Bulk select/deselect. scope=all flips every candidate across all pages;
    scope=page flips only the cids posted (the visible page)."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    from qobuz_librarian.web import job_persistence
    form = await request.form()
    on = (form.get("on") or "").strip().lower() in ("1", "true", "on", "yes")
    scope = (form.get("scope") or "all").strip().lower()
    cids = form.getlist("cid")[:100000] if scope == "page" else None
    # Tab scoping: on a library review, select-all flips only the active tab's
    # candidates — never the tab the user can't see.
    tab = (form.get("tab") or "").strip()
    if (cids is None and job.execute_kind == "library"
            and tab in ("missing", "gaps")):
        from qobuz_librarian.web import flows
        gap_active = tab == "gaps"
        with job._lock:
            cids = [c["cid"] for c in job.candidates
                    if flows.is_gap_candidate(c) == gap_active]
    if job.set_all_selected(on, cids=cids):
        await asyncio.get_running_loop().run_in_executor(
            None, job_persistence.persist, job)
        job.notify_review_changed()
    return JSONResponse(_selection_payload(job))


@app.post("/jobs/{job_id}/hide", response_class=HTMLResponse)
async def job_hide(request: Request, job_id: str):
    """Dismiss an artist's albums from a triage scan (gap or upgrade).

    A triage action, not a download — it writes the durable hidden-store (in
    the scan's scope) and drops those candidates from the review list,
    returning just the affected artist's group (or empty if the whole artist is
    gone) for an htmx swap of that one group. Allowed while the scan is still
    running, and never lock-guarded, so dismissing stays available mid-scan and
    while a download holds the staging lock.
    """
    # Use the disk fallback like every other review endpoint so Hide keeps
    # working on a restored/archived awaiting-review job (registry.get alone
    # 404s once the job is evicted, while /select, /review and /content don't).
    job = _get_reviewable_job(job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    if (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        from qobuz_librarian.web import flows
        form = await request.form()
        artist = (form.get("artist") or "").strip()
        # A library review split into Missing Albums / Gap Fill tabs scopes the
        # hide to the tab whose rows the button sat next to; the other tab's
        # candidates for this artist are untouched.
        tab = (form.get("tab") or "").strip()
        if job.execute_kind != "library" or tab not in ("missing", "gaps"):
            tab = ""
        gap_only = (tab == "gaps") if tab else None
        # Selection is server-backed, so hide keeps this artist's ticked albums
        # and drops the rest — no form keep-set, which under pagination would
        # only carry the visible page and clobber other pages' selections.
        n = flows.dismiss_albums(job, artist, scope=_hide_scope(job.execute_kind),
                                 gap_only=gap_only)
        if n:
            job.notify_review_changed()  # keep other open tabs in sync
        # Dismissing the last album completes the review — drop AWAITING_REVIEW so
        # the dashboard "new releases" banner clears and this page stops showing an
        # empty "awaiting review". HX-Refresh reloads to the finished view.
        if job_mgr.finalize_review_if_empty(job):
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        with job._lock:
            remaining = [c for c in job.candidates if c.get("artist") == artist
                         and (gap_only is None
                              or flows.is_gap_candidate(c) == gap_only)]
        if remaining:
            resp = _tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True, "review_tab": tab})
        else:
            resp = HTMLResponse("")  # whole artist hidden — outerHTML drops it
        if n:
            # Carry the fresh authoritative counts so the page updates the
            # summary/selected/reclaimable without recounting a partial DOM.
            import json as _json
            resp.headers["HX-Trigger"] = _json.dumps(
                {"qlHidden": {"n": n, "counts": _selection_payload(job)}})
        return resp
    return HTMLResponse("")


@app.post("/jobs/{job_id}/dismiss-rest")
async def job_dismiss_rest(request: Request, job_id: str):
    """Dismiss every album the user didn't pick: durable-hide all unselected
    candidates across the whole review at once, leaving just the keepers."""
    job = _get_reviewable_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        return JSONResponse({"error": "not reviewable"}, status_code=404)

    from qobuz_librarian.web import flows
    scope = _hide_scope(job.execute_kind)
    # Tab scoping: "Dismiss unselected" on a library review only drops the
    # active tab's unselected candidates.
    form = await request.form()
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    gap_only = (tab == "gaps") if tab else None
    # Snapshot the artists that still have an unticked album. dismiss_albums
    # re-reads each artist's saved ticks, so a tick that lands after this
    # snapshot is still honoured and its album isn't dropped.
    with job._lock:
        artists, seen = [], set()
        for c in job.candidates:
            if c.get("selected"):
                continue
            if gap_only is not None and flows.is_gap_candidate(c) != gap_only:
                continue
            name = c.get("artist") or ""
            if name not in seen:
                seen.add(name)
                artists.append(name)

    # Offload: this can touch the whole review (a hidden-store write per artist
    # plus a persist), which would block the event loop and stall every SSE
    # stream for a large scan.
    loop = asyncio.get_running_loop()
    hidden_count = await loop.run_in_executor(
        None, lambda: sum(flows.dismiss_albums(job, a, scope=scope,
                                               gap_only=gap_only)
                          for a in artists))
    if hidden_count:
        job.notify_review_changed()
    payload = _selection_payload(job)
    payload["hidden"] = hidden_count
    payload["review_done"] = job_mgr.finalize_review_if_empty(job)
    return JSONResponse(payload)


@app.post("/jobs/{job_id}/retry")
async def job_retry(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job or job.status != job_mgr.JobStatus.FAILED or not job.album_id:
        return RedirectResponse(url="/queue", status_code=303)
    album_id = job.album_id
    duplicate = _find_job_touching_album(album_id)
    if duplicate:
        return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
    try:
        token = _get_token()
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import get_album
        loop = asyncio.get_running_loop()
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(cfg.WEB_FETCH_TIMEOUT, get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        title = album.get("title") or job.title or "?"
        artist = (album.get("artist") or {}).get("name") or job.artist or "?"
        # Re-check for a duplicate under the submit lock: the await above yielded
        # the event loop, so a second Retry for the same album could have raced
        # in between the pre-check and here. Same guard queue_download uses, so
        # two quick retries can't double-queue the same album.
        with _DOWNLOAD_SUBMIT_LOCK:
            duplicate = _find_job_touching_album(album_id)
            if duplicate:
                return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
            # set_mode could have handed the lock to the terminal during the
            # get_album await above; re-check inside the submit lock (as
            # queue_download does) so a retry can't start a job after the CLI
            # handoff.
            busy = _lock_busy_response(request)
            if busy is not None:
                return busy
            # A failed single-track download carries job.album_id (so Retry shows up),
            # but _make_download_run would download the whole album. Rebuild it as
            # the same one-track run instead.
            single = getattr(job, "single", None)
            track = None
            if single and single.get("track_id"):
                tid = str(single.get("track_id"))
                track = next(
                    (t for t in (album.get("tracks") or {}).get("items") or []
                     if str(t.get("id")) == tid), None)
            new_job = job_mgr.Job(title=title, artist=artist, album_id=album_id)
            if track is not None:
                new_job.single = dict(single)
                job_mgr.submit(new_job, _make_single_track_run(album, track, token))
            elif single and single.get("track_id"):
                # The original was a single-track download but that track is no
                # longer on Qobuz — do NOT silently re-download the whole album.
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(
                        "That track is no longer on Qobuz. Nothing to retry."),
                    status_code=303)
            else:
                job_mgr.submit(new_job, _make_download_run(album, token))
        return RedirectResponse(url=f"/jobs/{new_job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote("Retry failed. Check your token."),
            status_code=303,
        )


@app.post("/jobs/{job_id}/undo")
async def job_undo(request: Request, job_id: str):
    """Reverse a single-track download: delete the track it added, drop the beets row
    for it, undo the single mark, and remove a folder the download created if it's
    now empty. Available while the job is still in memory."""
    # Undo deletes files and touches the beets DB, so it needs the same run-lock
    # gate every other mutating route has — the in-process staging lock below
    # can't keep it off the library while a CLI session or another instance
    # holds the cross-process lock.
    busy = _lock_busy_response(request)
    if busy is not None:
        if _is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">{busy.body.decode()}</div>')
        return busy
    job = job_mgr.registry.get(job_id)
    info = dict(getattr(job, "single", None) or {}) if job else {}
    if not job or not info.get("dir") or info.get("removed"):
        if _is_htmx(request):
            if job:
                return _tr(request, "_job_body.html", {"job": job})
            return HTMLResponse("", headers={"HX-Redirect": "/queue"})
        return RedirectResponse(url="/queue", status_code=303)

    def _refresh_after_undo():
        import logging

        from qobuz_librarian.web import flows

        artist = info.get("artist") or ""
        album = {
            "title": info.get("album") or "",
            "artist": {"name": artist},
        }
        try:
            flows._refresh_after_local_album_change(
                album,
                {"dir": info.get("dir") or ""},
                fallback_artist=artist,
                token=_get_optional_token(),
                args=flows.build_args(),
                upgrade=True,
                downsample=True,
            )
        except Exception as exc:
            logging.getLogger("qobuz_librarian").info(
                "quality state refresh after undo skipped: %s", exc)

    def _reverse():
        from pathlib import Path

        from qobuz_librarian.integrations.beets import forget_beets_entries
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.library.scanner import read_album_dir
        d = Path(info["dir"])
        want = (info.get("isrc") or "").replace("-", "").upper().strip()
        track_no = info.get("track_no")
        disc_no = info.get("disc_no")
        removed = None
        try:
            tracks = read_album_dir(d)
            if want:
                target = next(
                    (et for et in tracks
                     if (et.get("isrc") or "").replace("-", "").upper().strip() == want),
                    None)
            elif track_no is not None:
                # No ISRC to match on: fall back to the track number. A multi-disc
                # album can carry that same per-disc number on another disc, so
                # require the recorded disc to match — undo must remove the track
                # the download added, never its twin. A record from before the disc
                # was captured only deletes when the number is unique in the folder.
                numbered = [et for et in tracks
                            if et.get("tracknumber") == track_no]
                if disc_no is not None:
                    target = next(
                        (et for et in numbered
                         if (et.get("discnumber") or 1) == disc_no), None)
                else:
                    target = numbered[0] if len(numbered) == 1 else None
            else:
                target = None
            if target is not None:
                p = Path(target.get("path") or "")
                if p.exists():
                    p.unlink()
                    removed = p
        except OSError:
            pass
        if removed is not None:
            forget_beets_entries([removed])
            if info.get("marked"):
                hidden_mod.unmark_single(info.get("artist") or "", info.get("album") or "")
        # If the download created a brand-new folder and it now holds no audio, take
        # it back out so a one-off sample doesn't leave an empty album dir behind.
        try:
            if (info.get("new_folder") and d.is_dir()
                    and not any(x.is_file()
                                and x.suffix.lower() in cfg.AUDIO_EXTS
                                for x in d.rglob("*"))):
                import shutil
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
        # Return the Path actually deleted (or None when nothing matched) so the
        # caller can tell a real removal from a no-match — `removed is not None`
        # would collapse to a bool and make the not-found branch dead code,
        # reporting false success and burning the one-shot.
        return removed

    def _reverse_under_lock():
        # Take the lock inside the worker thread, never on the event loop —
        # holding a threading.Lock on the loop would freeze every other request
        # while a download worker (which may rip for minutes) holds it.
        with job_mgr.staging_lock():
            return _reverse()

    loop = asyncio.get_running_loop()
    removed = await loop.run_in_executor(None, _reverse_under_lock)
    if removed is not None:
        await loop.run_in_executor(None, _refresh_after_undo)
        job.single = {**info, "removed": True}
        job.summary = f"Removed “{info.get('title')}” and undid the single."
    else:
        # File not found at all (deleted externally) — still burn the one-shot
        # so Undo doesn't loop, but only when the dir is gone too. If the dir
        # exists but no track matched (ISRC/track_no mismatch), leave removed
        # unset so the user can attempt a manual fix and retry.
        from pathlib import Path as _Path
        dir_gone = not _Path(info["dir"]).exists()
        if dir_gone:
            if info.get("marked"):
                from qobuz_librarian.library import hidden as hidden_mod
                hidden_mod.unmark_single(info.get("artist") or "", info.get("album") or "")
            await loop.run_in_executor(None, _refresh_after_undo)
            job.single = {**info, "removed": True}
            job.summary = f"“{info.get('title')}” was already gone; cleared the single mark."
        else:
            job.summary = (f"Couldn't find “{info.get('title')}” by ISRC/track number. "
                           "Delete it manually if needed.")
    if _is_htmx(request):
        return _tr(request, "_job_body.html", {"job": job})
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    was_review = job.status == job_mgr.JobStatus.AWAITING_REVIEW
    was_pending = job.status == job_mgr.JobStatus.PENDING
    # Offload: cancelling a parked review runs cancel_review -> persist (a
    # json.dumps of the full candidate list + SQLite commit), which would block
    # the event loop and stall every SSE stream for a large review.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: job_mgr.request_cancel(job))
    # Repair stays on its single surface either way (idle start form once the
    # cancel lands). A queued job vanishes the instant it's cancelled, and a
    # review discard is instant too → back to the queue; a running/scanning job
    # stops cooperatively → keep them on the job page to watch it wind down.
    if job.execute_kind == "repair":
        dest = "/repair"
    elif job.execute_kind in _LIBRARY_SURFACE_KINDS:
        dest = "/library"
    else:
        dest = "/queue" if (was_review or was_pending) else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = ""):
    """The Queue tab: jobs in flight (pending / scanning / running / awaiting
    review). Finished jobs live in the History tab, which reads the durable
    archive rather than the capped in-memory set."""
    pending = job_mgr.registry.pending_and_running()
    return _tr(request, "queue.html", {
        "pending": pending,
        # Per-pending-job "waiting behind X" explainer, the same one the single
        # job page shows — so the Queue list says why a job hasn't started
        # instead of a bare "Queued". None for anything already running.
        "queue_waits": {j.id: _queue_wait(j) for j in pending},
        "error": error[:200],
        "page": "queue",
        "active_tab": "queue",
    })


_HISTORY_PER_PAGE = 30
# The card layer scrolls instead of paginating; enough for weeks of scans.
_HISTORY_BULK_CAP = 40


@app.get("/queue/history", response_class=HTMLResponse)
async def queue_history(request: Request, p: int = 1):
    """The History tab: every finished job, newest first, paged from jobs.db so
    the record outlives the in-memory cap (which only the Queue/SSE views use)."""
    from datetime import datetime

    from qobuz_librarian.web import job_persistence
    p = max(1, p)

    def _stamp(rows):
        for r in rows:
            ts = r.get("finished_at") or r.get("created_at")
            r["when"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        return rows

    def _load_page(page):
        # Two layers: meaningful jobs as a scrollable card region, plain
        # downloads as the paginated table underneath.
        bulk = _stamp(job_persistence.history_page(_HISTORY_BULK_CAP, 0, bulk=True))
        total = job_persistence.history_count(bulk=False)
        pages = max(1, (total + _HISTORY_PER_PAGE - 1) // _HISTORY_PER_PAGE)
        page = min(max(1, page), pages)
        rows = _stamp(job_persistence.history_page(
            _HISTORY_PER_PAGE, (page - 1) * _HISTORY_PER_PAGE, bulk=False))
        return bulk, total, pages, page, rows

    loop = asyncio.get_running_loop()
    bulk_jobs, total, pages, p, rows = await loop.run_in_executor(None, lambda: _load_page(p))
    retryable_ids = [
        j.id for j in job_mgr.registry.all()
        if j.status == job_mgr.JobStatus.FAILED and j.album_id
    ]
    return _tr(request, "history.html", {
        "page": "queue", "active_tab": "history",
        "bulk_jobs": bulk_jobs, "jobs": rows,
        "cur_page": p, "pages": pages, "total": total,
        "retryable_ids": retryable_ids,
    })


@app.post("/queue/clear")
async def queue_clear():
    """Clear the History: drop finished/canceled/failed jobs from the registry
    and the full on-disk archive. In-flight jobs are untouched."""
    from qobuz_librarian.web import job_persistence
    job_mgr.registry.clear_finished()
    job_persistence.clear_history()
    return RedirectResponse(url="/queue/history", status_code=303)


@app.post("/queue/cancel-pending")
async def queue_cancel_pending():
    for j in list(job_mgr.registry.pending_and_running()):
        job_mgr.request_cancel(j)
    return RedirectResponse(url="/queue", status_code=303)


def _diagnostics():
    """Read-only health checks surfaced on the Settings page."""
    import os
    import shutil as _sh

    checks = []

    def _dir_check(label, path, *, want_writable):
        p = Path(path)
        if not p.exists():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} does not exist (volume not mounted?)"})
            return
        if not p.is_dir():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} exists but is not a directory"})
            return
        if want_writable and not os.access(p, os.W_OK):
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} is not writable by the container user. "
                           "On a NAS, set PUID/PGID in .env to your media-share owner"})
            return
        try:
            n = sum(1 for _ in p.iterdir())
        except OSError as e:
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} unreadable: {e}"})
            return
        checks.append({"label": label, "ok": True,
                       "detail": f"{p} — {n} entr{'y' if n == 1 else 'ies'}"})

    _dir_check("Music library", cfg.MUSIC_ROOT, want_writable=True)
    _dir_check("Staging area", cfg.STAGING_DIR, want_writable=True)

    beets_db = Path(cfg.BEETS_DB_PATH)
    if beets_db.exists():
        ok = os.access(beets_db, os.R_OK)
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": ok,
                       "detail": f"{beets_db}" if ok
                       else f"{beets_db} exists but is not readable"})
    elif beets_db.parent.exists():
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": True,
                       "detail": f"{beets_db} (created on first import)"})
    else:
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": False,
                       "detail": f"{beets_db.parent} does not exist"})

    for binary in ("rip", "beet", "ffmpeg", "flac"):
        found = _sh.which(binary)
        checks.append({"label": f"`{binary}` binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} not on PATH. "
                       "Rebuild the image (docker compose build)"})

    stranded = []
    if cfg.UPGRADE_BACKUP_DIR.exists():
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if entry.is_dir() and (entry.suffix == ".partial"
                                       or entry.name == ".restore_trash"):
                    stranded.append(entry)
        except OSError:
            pass
    if stranded:
        checks.append({"label": "Stranded upgrade backups", "ok": False,
                       "detail": f"{len(stranded)} found in "
                                 f"{cfg.UPGRADE_BACKUP_DIR}; manual cleanup needed"})
    else:
        checks.append({"label": "Stranded upgrade backups", "ok": True,
                       "detail": "none"})

    # Backups whose original is still missing the tracks they hold — orphaned by
    # a hard kill that skipped the restore/delete. Retention keeps these rather
    # than reaping the only copy; surface them so they can be reconciled.
    try:
        from qobuz_librarian.library.backup import find_only_copy_backups
        orphans = find_only_copy_backups()
    except Exception:
        orphans = []
    if orphans:
        first = orphans[0]
        hint = f" e.g. restore {first[0].name!r} → {first[1]}" if first[1] else ""
        checks.append({"label": "Orphaned backups (only copy)", "ok": False,
                       "detail": f"{len(orphans)} backup(s) hold tracks missing "
                                 f"from their album folder.{hint}"})
    else:
        checks.append({"label": "Orphaned backups (only copy)", "ok": True,
                       "detail": "none"})
    return checks


def _resolve_host_path(container_path: str) -> tuple[str, bool]:
    """Return (display_path, is_host_path) for a path inside the container.

    Walks /proc/self/mountinfo to find the longest-prefix bind mount, then
    appends the remaining suffix to the host source. Falls back to the
    container path when no bind mount covers it (anonymous volume) or the
    file isn't available (non-Linux).
    """
    container_path = str(container_path)
    try:
        with open("/proc/self/mountinfo") as f:
            entries = []
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                entries.append((parts[4], parts[3]))  # mount_point, host_root
    except OSError:
        return container_path, False
    best = None
    for mount_point, host_root in entries:
        if mount_point == "/":  # container rootfs, not a user bind mount
            continue
        if (container_path == mount_point
                or container_path.startswith(mount_point.rstrip("/") + "/")):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, host_root)
    if best is None:
        return container_path, False
    mount_point, host_root = best
    suffix = container_path[len(mount_point):]
    host_path = host_root.rstrip("/") + suffix if suffix else host_root
    return host_path, True


def _settings_response(request, *, saved=False, queued=False, connected=False,
                       unverified=False, error="", mode="", user_id=None,
                       auth_token_prefill="", diagnostics=None, warnings=None):
    from qobuz_librarian.web import settings_store
    creds = _read_creds()
    values = settings_store.current()
    # If credentials come from QOBUZ_USER_AUTH_TOKEN env, anything saved
    # via the form is overridden on next process start — let the user know.
    import os
    # cfg.QOBUZ_USER_AUTH_TOKEN resolves QOBUZ_USER_AUTH_TOKEN_FILE too (the
    # secret isn't re-exported to os.environ), so a *_FILE deployment is
    # correctly recognised as env-provided.
    creds_from_env = bool(cfg.QOBUZ_USER_AUTH_TOKEN)
    cli_only_env = os.environ.get("QL_CLI_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on")
    music_storage = None
    try:
        du = shutil.disk_usage(cfg.MUSIC_ROOT)
        def _tb(n):
            return f"{n / 1e12:.2f} TB" if n >= 1e12 else f"{n / 1e9:.0f} GB"
        music_storage = {
            "used": _tb(du.used), "free": _tb(du.free),
            "pct": round(du.used / du.total * 100, 1) if du.total else 0,
        }
    except OSError:
        pass
    return _tr(request, "settings.html", {
        "music_storage": music_storage,
        "user_id": creds.get("user_id", "") if user_id is None else user_id,
        "auth_token_set": bool(creds.get("auth_token")),
        "auth_token_prefill": auth_token_prefill,
        "creds_from_env": creds_from_env,
        "cli_only_env": cli_only_env,
        "mode_changed": (mode or "").strip().lower(),
        "saved": saved,
        "queued": queued,
        "connected": connected,
        "unverified": unverified,
        "error": error,
        "warnings": warnings or [],
        "page": "settings",
        "library_paths": [
            {"label": label, "container": cp,
             "host": host, "resolved": resolved}
            for label, cp in (
                ("Music library", cfg.MUSIC_ROOT),
                ("Staging area", cfg.STAGING_DIR),
                ("Beets database", cfg.BEETS_DB_PATH),
                ("Streamrip config", cfg.STREAMRIP_CONFIG),
            )
            for host, resolved in [_resolve_host_path(cp)]
        ],
        "behavior_fields": settings_store.BEHAVIOR_FIELDS,
        "text_fields": settings_store.TEXT_FIELDS,
        "option_labels": settings_store.ENUM_OPTION_LABELS,
        "behavior": values,
        "diagnostics": diagnostics if diagnostics is not None else _diagnostics(),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False,
                        queued: bool = False, connected: bool = False,
                        unverified: bool = False, error: str = "",
                        mode: str = ""):
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    return _settings_response(request, saved=saved, queued=queued,
                              connected=connected, unverified=unverified,
                              error=error, mode=mode, diagnostics=diags)


def _streamrip_has_userid() -> bool:
    """True if the streamrip config carries a non-empty user id, so `rip` can
    actually authenticate a download. A token-only env (QOBUZ_USER_AUTH_TOKEN set,
    QOBUZ_USER_ID unset) has none until the id is set or creds are saved, even
    though the app's own Qobuz API calls work from the token alone."""
    try:
        if not cfg.STREAMRIP_CONFIG.exists():
            return False
        import tomllib
        with open(cfg.STREAMRIP_CONFIG, "rb") as f:
            data = tomllib.load(f)
        uid = str(data.get("qobuz", {}).get("email_or_userid", "") or "").strip()
        return bool(uid)
    except Exception:
        return False


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, user_id: str = Form(""), auth_token: str = Form("")):
    global _TOKEN_VALID
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    existing = _read_creds()
    # First-run with empty inputs: nothing to save and no creds to keep —
    # bounce back with a banner rather than writing blanks and flashing green.
    if not auth_token.strip() and not user_id.strip() \
            and not existing.get("auth_token") \
            and not cfg.QOBUZ_USER_AUTH_TOKEN:
        return RedirectResponse(url="/settings?error=empty", status_code=303)
    # Blank means "keep the existing value" — the fields are not pre-filled,
    # so an empty submission must not wipe a previously-saved credential.
    if not auth_token.strip() and not user_id.strip() and cfg.QOBUZ_USER_AUTH_TOKEN:
        # Blank submit with an env token = "keep the env creds". But downloads
        # shell out to `rip`, which also needs a user id; a token-only env
        # authenticates our own API calls yet fails every download. Only report
        # connected when a usable user id actually exists (env id, or one already
        # in the rip config) — otherwise show the needuser banner instead of a
        # false green that dead-ends at the first download.
        if cfg.QOBUZ_USER_ID or _streamrip_has_userid():
            return RedirectResponse(url="/settings?connected=1", status_code=303)
        return _settings_response(request, error="needuser", user_id="",
                                  auth_token_prefill="", diagnostics=diags)
    new_token = auth_token.strip() or existing.get("auth_token", "")
    new_uid = user_id.strip() or existing.get("user_id", "")
    # Both fields are mandatory. The token authenticates our API calls on its
    # own, so the check below passes with just a token — but load_qobuz_token()
    # and streamrip's login() both require the user id, so a token-only save
    # would look connected yet fail with "no credentials" on the first search,
    # and downloads would raise MissingCredentialsError. Refuse a half-config
    # and name the missing field. Re-render rather than redirect on these two:
    # the user typed something that didn't save, and a fresh GET can't pre-fill
    # the password field — so a redirect would wipe the (long) token they just
    # pasted.
    if new_token and not new_uid:
        return _settings_response(request, error="needuser",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    if new_uid and not new_token:
        return _settings_response(request, error="empty",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    # Check the token with Qobuz *before* writing it. A token Qobuz outright
    # rejects never lands in the config — we re-render with it still in the box
    # so the user can fix a paste slip without losing it. A network/timeout
    # failure can't tell us either way, so we save and flag it unverified.
    verdict = "unreachable"
    if new_token:
        from qobuz_librarian.api.client import call_within
        try:
            verdict = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                              _classify_token, new_token)),
                timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            verdict = "unreachable"
    if verdict == "rejected":
        # Re-render with the real token still in the (password-type, so
        # visually masked) field so the user can fix a paste slip without
        # re-typing it — same as the needuser/empty/creds branches.
        return _settings_response(request, error="rejected",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    ok = _write_creds(new_uid, new_token)
    if not ok:
        return _settings_response(request, error="creds",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    # Keep the dashboard's "token isn't authenticating" banner in step with
    # what we just verified — a freshly-fixed token shouldn't keep nagging
    # until the next restart. An unverified save drops back to inconclusive
    # rather than leaving a stale False from an earlier probe.
    _TOKEN_VALID = True if verdict == "ok" else None
    suffix = "&unverified=1" if verdict == "unreachable" else ""
    return RedirectResponse(url=f"/settings?connected=1{suffix}", status_code=303)


@app.post("/settings/behavior", response_class=HTMLResponse)
async def save_behavior(request: Request):
    from qobuz_librarian.web import settings_store
    form = await request.form()
    def _posted_bool(key):
        return form.get(key, "").strip().lower() not in (
            "0", "false", "off", "no", ""
        )
    # The real Settings form ships a hidden form_complete=1 marker. When
    # it's present, every checkbox key is known to be authoritative
    # (unchecked = absent = False). When it's absent — a scripted partial
    # POST — only the keys the caller actually sent overwrite; the rest
    # are left at their current value so a one-field toggle doesn't blow
    # away the user's other booleans.
    is_complete = "form_complete" in form
    if is_complete:
        values = {k: (_posted_bool(k) if k in form else False)
                  for k in settings_store.BEHAVIOR_KEYS}
    else:
        values = {k: _posted_bool(k)
                  for k in settings_store.BEHAVIOR_KEYS if k in form}
    # Text/enum/list fields: take whatever the form posted; absent =
    # leave unchanged (don't wipe a previously-set value).
    for k in settings_store.TEXT_KEYS:
        if k in form:
            values[k] = form.get(k, "")
    ok, warnings = settings_store.save(values)
    # Applied in-memory regardless; error only means it won't persist.
    if not ok:
        return RedirectResponse(url="/settings?error=persist", status_code=303)
    if warnings:
        # Re-render in place so we can name exactly which entries were dropped
        # (a misspelt provider, an uninstalled beets plugin) without smuggling
        # user-typed values through the redirect URL.
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, _diagnostics)
        return _settings_response(request, saved=True,
                                  queued=settings_store._any_active_job(),
                                  warnings=warnings, diagnostics=diags)
    suffix = "&queued=1" if settings_store._any_active_job() else ""
    return RedirectResponse(url=f"/settings?saved=1{suffix}", status_code=303)


@app.post("/settings/mode")
async def set_mode(request: Request, target: str = Form("")):
    """Hand the run-lock to the terminal (CLI), or take it back for the web.

    Switching to CLI is refused while a download/scan is active — releasing the
    lock under a running job would let the CLI race the worker over /staging.
    """
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE, _creds_cache
    from qobuz_librarian import run_lock
    want = (target or "").strip().lower()
    if want == "cli":
        # Flip to CLI mode first so a /download or scan POST landing during the
        # handoff is refused (503) instead of slipping past the check and racing
        # the CLI over /staging once we release the lock below.
        _CLI_MODE = True
        if job_mgr.registry.pending_and_running():
            _CLI_MODE = False  # no transfer happened; stay in web mode
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "Finish or cancel the active download before handing off to the "
                "terminal."), status_code=303)
        if _RUN_LOCK_HANDLE is not None:
            try:
                _RUN_LOCK_HANDLE.close()  # closing the handle releases the flock
            except OSError:
                pass
            _RUN_LOCK_HANDLE = None
        _LOCK_BUSY_PID = None
        return RedirectResponse(url="/settings?mode=cli", status_code=303)
    if want == "web":
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire()
            _CLI_MODE = False
            _LOCK_BUSY_PID = None
            # The CLI may have changed the saved token while it held the lock;
            # drop the cached creds so the banner reflects what's on disk now.
            _creds_cache = None
            return RedirectResponse(url="/settings?mode=web", status_code=303)
        except run_lock.LockBusy:
            # A CLI session still holds the lock — can't take it back yet.
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "The terminal is still using it. Finish your CLI command, then "
                "resume."), status_code=303)
    return RedirectResponse(url="/settings", status_code=303)


# Empty 500ms ticks before we emit a `: ping` heartbeat to keep
# reverse proxies from dropping the EventSource on a quiet scan.
# Defaults from cfg.SSE_HEARTBEAT_TICKS / cfg.SSE_MAX_WORKERS (env-tunable).
_SSE_HEARTBEAT_TICKS = cfg.SSE_HEARTBEAT_TICKS

# Dedicated thread pool for SSE waits so a long-running scan with many
# tabs open doesn't starve /search and /download on the default executor.
_SSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=cfg.SSE_MAX_WORKERS, thread_name_prefix="sse")


@app.get("/api/diagnostics", response_class=HTMLResponse)
async def api_diagnostics(request: Request):
    """Htmx partial — returns just the diagnostics list items for the Recheck button."""
    loop = asyncio.get_running_loop()
    checks = await loop.run_in_executor(None, _diagnostics)
    rows = []
    for d in checks:
        icon = "OK" if d["ok"] else "!"
        cls = "ql-diagnostic-status-ok" if d["ok"] else "ql-diagnostic-status-error"
        aria = "OK" if d["ok"] else "Needs attention"
        detail = f'<div class="ql-diagnostic-detail">{html.escape(d.get("detail") or "")}</div>' if d.get("detail") else ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status {cls}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">{html.escape(d["label"])}</div>{detail}</div>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _generator():
        import logging as _logging
        import queue as _queue
        # Reconnect quickly so a backgrounded tab's progress bar catches up to
        # the live count soon after it's brought back to the foreground.
        yield "retry: 750\n\n"
        if (job.status in job_mgr.TERMINAL
                or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
            for line in job.log_lines[-job.REPLAY_TAIL:]:
                escaped = line.replace("\n", " ").replace("\r", "")
                yield f"data: {escaped}\n\n"
            yield f"event: done\ndata: {job.status.value}\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    empty_ticks = 0
                    if line == job_mgr.STREAM_END:
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    if line.startswith(job_mgr.PROGRESS_PREFIX):
                        yield ("event: progress\ndata: "
                               + line[len(job_mgr.PROGRESS_PREFIX):] + "\n\n")
                        continue
                    if line == job_mgr.REVIEW_CHANGED:
                        continue  # review-sync nudge — handled by the review stream
                    escaped = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {escaped}\n\n"
                except _queue.Empty:
                    if (job.status in job_mgr.TERMINAL
                            or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logging.getLogger("qobuz_librarian").exception(
                        "SSE stream error for job %s", job.id)
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/review-stream")
async def job_review_stream(job_id: str):
    """Live channel for an awaiting-review page: emits `event: review` whenever
    selection or candidates change (a tick/untick/hide in this or another tab),
    so every open view stays in sync. Closes once the job leaves review (the
    page then reloads to show the executing/finished state). Separate from the
    progress stream, which closes the moment a scan finishes."""
    # Only a LIVE job (in the registry) has a producer that fans out review
    # nudges; a historical/evicted review still renders and saves selection via
    # the disk fallback, but can't receive live cross-tab updates — so end its
    # stream cleanly rather than 404 (which surfaces as a console error) or hold
    # a socket that never gets a nudge.
    job = job_mgr.registry.get(job_id)

    async def _generator():
        import queue as _queue
        yield "retry: 1000\n\n"
        if job is None or job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            yield "event: closed\ndata: inactive\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    if line == job_mgr.REVIEW_CHANGED:
                        yield "event: review\ndata: changed\n\n"
                    # All other fanned-out lines (log/progress/end) are ignored
                    # here — this channel only carries review-sync nudges.
                except _queue.Empty:
                    if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                        yield f"event: closed\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _job_to_dict(job, *, log_tail: int = 50):
    out = {
        "id": job.id,
        "status": job.status.value,
        "title": job.title,
        "artist": job.artist,
        "album_id": getattr(job, "album_id", None),
        "error": job.error,
        "created_at": getattr(job, "created_at", None),
        "finished_at": getattr(job, "finished_at", None),
    }
    if log_tail:
        out["log_lines"] = job.log_lines[-log_tail:]
    return out


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        # A finished job evicted past MAX_FINISHED is still on disk; fall back
        # to the archive so a poller gets its terminal status instead of a 404
        # (mirrors how GET /jobs/{job_id} rehydrates from history).
        job = job_mgr.load_historical_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@app.get("/api/queue/count")
async def queue_count():
    """Live count of active jobs (pending/scanning/running/awaiting-review) so the
    nav Queue badge stays in sync without a page reload. The badge is otherwise
    server-rendered once per page, which left it stale (e.g. reading "1" next to
    an empty Queue) after a job finished while you sat on another page."""
    active = job_mgr.registry.pending_and_running()
    return JSONResponse({
        "count": len(active),
        "running": any(j.status.value in ("running", "scanning") for j in active),
    })


@app.get("/api/jobs")
async def jobs_list(status: str = "", limit: int = 50):
    """List jobs as JSON. Optional `status` filter ('pending', 'running',
    'awaiting_review', 'scanning', 'done', 'failed', 'canceled').
    `limit` caps the response — most recent first.

    Live (non-terminal) jobs come from the in-memory registry. Terminal jobs
    (done/failed/canceled) come from the registry too, but it only keeps the
    most-recent MAX_FINISHED of them, so we also reach into the on-disk archive
    to surface jobs evicted past that cap — otherwise `status=done` could never
    return anything older than the last ~50 finishes."""
    wanted = status.strip().lower() or None
    if wanted is not None:
        valid = {s.value for s in job_mgr.JobStatus}
        if wanted not in valid:
            raise HTTPException(status_code=400,
                                detail="Unknown status filter")
    cap = max(1, min(limit, 500))
    terminal_values = {s.value for s in job_mgr.TERMINAL}
    want_terminal = wanted in terminal_values if wanted else True

    matching = []
    seen = set()
    for j in reversed(job_mgr.registry.all()):
        if wanted and j.status.value != wanted:
            continue
        matching.append(_job_to_dict(j, log_tail=0))
        seen.add(j.id)
        if len(matching) >= cap:
            break

    # The registry only holds the newest MAX_FINISHED terminal jobs; the archive
    # keeps far more. Append older finished rows (deduped) so they're reachable.
    # Registry rows are the live copy and already cover the newest finishes, so
    # the archive rows we add are strictly older and stay correctly ordered.
    if want_terminal and len(matching) < cap:
        from qobuz_librarian.web import job_persistence
        for row in job_persistence.history_page(cap, 0):
            if wanted and row["status"] != wanted:
                continue
            if row["id"] in seen:
                continue
            matching.append({
                "id": row["id"],
                "status": row["status"],
                "title": row["title"],
                "artist": row["artist"],
                "album_id": row["album_id"] or None,
                "error": row["error"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            })
            seen.add(row["id"])
            if len(matching) >= cap:
                break

    return JSONResponse({"jobs": matching, "count": len(matching)})


def _get_token():
    from qobuz_librarian.api.auth import load_qobuz_token
    return load_qobuz_token()[1]


def _get_optional_token():
    if not _read_creds().get("auth_token"):
        return None
    try:
        return _get_token()
    except Exception:
        return None


def _format_age(ts: float) -> str:
    """Human-readable age of a past timestamp."""
    import time as _time
    age = _time.time() - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400:
        return f"{int(age / 3600)} hr ago"
    days = int(age / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _last_scan_age() -> str | None:
    """Human-readable age of the last library/artist scan, or None."""
    try:
        ts = float(cfg.LAST_SCAN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return _format_age(ts)


def _last_new_release_check_age() -> str | None:
    """Human-readable age of the last new-release check, or None — gives the
    dashboard a sibling indicator to the existing 'last library scan' line so
    the user can see how fresh the auto-check's signal is."""
    from qobuz_librarian.library import new_releases
    ts = new_releases.last_run()
    return _format_age(ts) if ts is not None else None


def _tool_last_run_age(execute_kind: str) -> str | None:
    """Age of the last clean run of a tool scan, or None if it's never
    finished — so a tool page can show "Last scan 3 days ago" instead of
    looking identical to a first visit."""
    from qobuz_librarian.web import job_persistence
    ts = job_persistence.last_finished_at(execute_kind)
    return _format_age(ts) if ts is not None else None


def _no_creds_response(request):
    """Return a 303 redirect (or htmx fragment) when no credentials are set."""
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html(
                "error",
                'No Qobuz credentials set. Visit '
                '<a href="/settings" class="ql-inline-link">Settings</a>.',
            ),
            status_code=200)
    return RedirectResponse(url="/settings?error=creds", status_code=303)


_creds_cache: dict | None = None


def _read_creds():
    global _creds_cache
    # cfg resolves QOBUZ_USER_AUTH_TOKEN_FILE too (the secret is no longer
    # re-exported to os.environ), so a *_FILE deployment is recognised here.
    env_token = cfg.QOBUZ_USER_AUTH_TOKEN
    if env_token:
        return {"user_id": cfg.QOBUZ_USER_ID or "", "auth_token": env_token}
    if _creds_cache is not None:
        return _creds_cache
    if not cfg.STREAMRIP_CONFIG.exists():
        return {}
    try:
        import tomllib
        with open(cfg.STREAMRIP_CONFIG, "rb") as f:
            data = tomllib.load(f)
        qz = data.get("qobuz", {})
        _creds_cache = {"user_id": qz.get("email_or_userid", ""),
                        "auth_token": qz.get("password_or_token", "")}
        return _creds_cache
    except Exception:
        return {}


def _write_creds(user_id, auth_token) -> bool:
    """Write credentials into the streamrip config. Returns False if the
    config volume isn't writable (NAS perms) so the Settings page can show
    a clear message rather than 500ing.

    Delegates to qobuz_librarian.api.auth.write_streamrip_creds so the web
    Settings path and the env-var sync share one credential writer."""
    global _creds_cache
    _creds_cache = None
    from qobuz_librarian.api.auth import write_streamrip_creds
    return write_streamrip_creds(user_id, auth_token)


def start():
    import uvicorn
    # server_header=False mirrors the --no-server-header the Docker entrypoint
    # passes, so the installed qobuz-librarian-web entrypoint doesn't advertise
    # "Server: uvicorn" (a free hint to anyone scanning for framework CVEs).
    uvicorn.run("qobuz_librarian.web.app:app", host=cfg.WEB_HOST,
                port=cfg.WEB_PORT, workers=1, server_header=False)
