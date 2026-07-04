"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts — a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.library import (
    downsample_state,
    library_scan_state,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    find_album_dir_filesystem,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
    flush_resolve_cache,
    resolve_artist_dir,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.web import review_badges


def build_args():
    """Namespace of CLI flags used by process_album and the artist/walk runners.

    `consolidate` is forced False: the web has no confirm() UI, so letting
    the engine scan for siblings it can't act on would waste time.
    """
    return argparse.Namespace(
        force=False, yes=True, dry_run=False, no_import=False,
        no_upgrade=False, no_downsample=False,
        prefer_hires=cfg.PREFER_HIRES,
        consolidate=False,
        migrate_multi_artist=cfg.MIGRATE_MULTI_ARTIST,
        include_comps=False,
        include_singles=False,
        no_catalog=False,
        auto_safe=False,
        # auto_upgrade is request-scoped (the explicit Upgrade flow flips it
        # to True for one run) so passive gap scans do not need to mutate
        # cfg.AUTO_UPGRADE_ENABLED mid-job.
        auto_upgrade=cfg.AUTO_UPGRADE_ENABLED,
        verbose=False,
    )


def _set_empty_library_summary(job):
    job.summary = (
        "No artist folders were found in the configured music library. "
        "Check that it contains your artist folders."
    )
    log.info("No artist folders found in the configured music library.")
    log.info("  Expected layout: <music library>/<Artist>/<Album (Year)>/<track>.flac")
    log.info("  Check the music library path in Settings.")


def _surface_has_candidates(surface):
    if surface == "upgrade":
        return upgrade_state.has_visible_candidates()
    elif surface == "downsample":
        return downsample_state.has_visible_candidates()
    return False


def _artist_dir_from_result(album, result=None, fallback_artist=None):
    result_dir = (result or {}).get("dir")
    if result_dir:
        album_dir = Path(result_dir)
        if album_dir.exists():
            return album_dir.parent if album_dir.is_dir() else album_dir.parent.parent
    if album:
        try:
            clear_scan_caches()
            album_dir = find_album_dir_filesystem(album)
            if album_dir and album_dir.exists():
                return album_dir.parent
        except Exception as exc:
            log.info(f"  state refresh path lookup failed: {exc}")
    if fallback_artist:
        try:
            return resolve_artist_dir(fallback_artist)
        except Exception as exc:
            log.info(f"  state refresh artist lookup failed: {exc}")
    return None


def _refresh_downsample_artist_state(artist_dir):
    if artist_dir is None:
        return
    result = downsample_state.update_artist(artist_dir, hidden=hidden_mod.load())
    if result.complete:
        review_badges.set_ready("downsample", _surface_has_candidates("downsample"))
    else:
        err = next(iter(result.errors.values()), "unknown error")
        log.info(f"  downsample view refresh skipped for {artist_dir.name}: {err}")


def _refresh_upgrade_artist_state(artist_dir, token, args=None):
    if not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
        return
    if artist_dir is None or not token:
        return
    try:
        from qobuz_librarian.quality.decision import load_capped
        result = upgrade_state.update_artist(
            artist_dir,
            token=token,
            args=args or build_args(),
            capped=load_capped(),
            hidden=hidden_mod.load(),
        )
    except (AuthLost, QobuzUnavailable):
        raise
    if result.complete:
        review_badges.set_ready("upgrade", _surface_has_candidates("upgrade"))
    else:
        err = next(iter(result.errors.values()), "unknown error")
        log.info(f"  upgrade view refresh skipped for {artist_dir.name}: {err}")


def _refresh_after_local_album_change(
    album,
    result=None,
    *,
    fallback_artist=None,
    token=None,
    args=None,
    upgrade=False,
    downsample=False,
):
    artist_dir = _artist_dir_from_result(album, result, fallback_artist)
    if artist_dir is None:
        return
    if upgrade:
        _refresh_upgrade_artist_state(artist_dir, token, args=args)
    if downsample:
        _refresh_downsample_artist_state(artist_dir)


def _album_cover(album):
    """The album's small cover URL, only if it's a trusted Qobuz CDN link."""
    img = album.get("image") or {}
    url = img.get("small") or img.get("thumbnail") or ""
    return url if url.startswith("https://static.qobuz.com/") else ""


def _album_candidate_spec(
    album,
    artist_name,
    selected=True,
    is_new=False,
    extra_payload=None,
):
    year = album_year(album)
    partial_n = album.get("_partial_missing_count")
    if partial_n:
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"gap-fill: {partial_n} missing of "
                  f"{album.get('tracks_count') or '?'}")
    else:
        tc = album.get('tracks_count')
        n = int(tc) if str(tc or '').isdigit() else None
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"{n if n is not None else '?'} track{'' if n == 1 else 's'}")
    payload = {"album_id": album.get("id"), "year": year, "cover": _album_cover(album)}
    if partial_n:
        payload["gap_fill"] = partial_n
    if extra_payload:
        payload.update(extra_payload)
    if is_new:
        payload["is_new"] = True
    return {
        "kind": "album",
        "title": album.get("title") or "?",
        "artist": artist_name,
        "detail": detail,
        "payload": payload,
        "selected": selected,
    }


def _add_candidate_spec(job, spec):
    return job.add_candidate(
        kind=spec.get("kind", "album"),
        title=spec.get("title") or "?",
        artist=spec.get("artist") or "",
        detail=spec.get("detail") or "",
        payload=spec.get("payload") or {},
        selected=bool(spec.get("selected")),
    )


def _readd_candidate(job, c):
    """Re-add a candidate restored from a scan checkpoint, with a fresh cid."""
    _add_candidate_spec(job, c)


def _cap_note(job) -> str:
    """A truncation notice appended to a scan summary when the candidate list hit
    the in-memory cap, so a summary never implies more results are reviewable
    than were actually kept. Empty when nothing was dropped."""
    if not job.candidate_cap_hit:
        return ""
    return (f" Showing the first {len(job.candidates):,}; the scan hit the "
            f"{job.CANDIDATE_CAP:,} result cap. Scan a single artist, or raise "
            "JOB_CANDIDATE_CAP, to see the rest.")


def _gap_candidate_spec(
    gap,
    artist_name,
    selected=False,
    is_new=False,
    artist_key=None,
):
    """Turn an engine AlbumGap into a review candidate. A partial gap carries
    its missing-track count so the detail reads 'gap-fill: N missing'."""
    album = gap.qobuz_album
    if gap.on_disk_dir is not None:
        album = {**album, "_partial_missing_count": gap.missing_count}
    extra_payload = {"_artist_dir": artist_key} if artist_key else None
    return _album_candidate_spec(
        album, artist_name, selected=selected, is_new=is_new,
        extra_payload=extra_payload)


def _add_gap_candidate(job, gap, artist_name, selected=False, is_new=False):
    return _add_candidate_spec(
        job, _gap_candidate_spec(gap, artist_name, selected, is_new))


def is_gap_candidate(c):
    """Whether a saved review candidate is a Gap Fill entry (missing tracks in
    an owned album) rather than a fully missing album. New scans stamp the
    payload; candidates carried forward from older checkpoints only say so in
    their detail line, so fall back to that."""
    if (c.get("payload") or {}).get("gap_fill"):
        return True
    return "gap-fill:" in (c.get("detail") or "")


def fold_new_candidates(parked, cands):
    """Append candidates a parked review doesn't already list, keyed by Qobuz
    album id (falling back to artist+title for keyless carry-overs). The
    parked review's own entries — and the user's ticks on them — are never
    touched; a refresh only ever adds. Returns how many were added."""
    def _key(c):
        album_id = str((c.get("payload") or {}).get("album_id") or "")
        if album_id:
            return album_id
        return ((c.get("artist") or "").lower(), (c.get("title") or "").lower())

    with parked._lock:
        seen = {_key(c) for c in parked.candidates}
    added = 0
    for c in cands:
        key = _key(c)
        if key in seen:
            continue
        seen.add(key)
        if _add_candidate_spec(parked, c) is not None:
            added += 1
    return added


def prune_library_review_candidates(album):
    """A full album just landed on disk (Search download, batch download,
    upgrade replace): drop its candidates from every parked or still-scanning
    library review, so a stale review can't offer to download an album the
    user already has. The executing job itself is RUNNING and untouched.
    Matched by Qobuz album id. Returns the number of candidates dropped."""
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr
    album_id = str((album or {}).get("id") or "")
    if not album_id:
        return 0
    dropped = 0
    states = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    for job in job_mgr.registry.all():
        if (getattr(job, "execute_kind", "") not in ("library", "new_releases")
                or job.status not in states):
            continue
        try:
            with job._lock:
                keep = [c for c in job.candidates
                        if str((c.get("payload") or {}).get("album_id") or "")
                        != album_id]
                n = len(job.candidates) - len(keep)
                if not n:
                    continue
                job.candidates = keep
            dropped += n
            job_persistence.persist(job)
            job.notify_review_changed()
            job_mgr.finalize_review_if_empty(job)
        except Exception as e:
            # Pruning is housekeeping on the side of a successful download —
            # never let it turn that success into a failure.
            log.info(f"  couldn't prune review {job.id}: {e}")
    return dropped


def drop_owned_missing_candidates(job):
    """Reconcile a parked library review against the disk right before it
    executes: a missing-album candidate whose folder now exists (grabbed from
    Search while the review sat parked, or added by hand) is dropped instead
    of downloaded again. Gap Fill candidates are left alone — their album
    folder exists by definition; full-album imports already prune them via
    prune_library_review_candidates. Returns how many of the dropped
    candidates were selected (the number the user believes they're about to
    download)."""
    from qobuz_librarian.library.catalog import find_album_dir_filesystem
    from qobuz_librarian.web import job_persistence
    with job._lock:
        snapshot = [(c["cid"], bool(c.get("selected")),
                     {"id": (c.get("payload") or {}).get("album_id"),
                      "title": c.get("title") or "",
                      "artist": {"name": c.get("artist") or ""}})
                    for c in job.candidates if not is_gap_candidate(c)]
    # Disk probes happen outside the lock; a live scan can keep appending.
    owned = {}
    for cid, selected, alb in snapshot:
        try:
            if find_album_dir_filesystem(alb) is not None:
                owned[cid] = selected
        except Exception:
            continue
    if not owned:
        return 0
    with job._lock:
        job.candidates = [c for c in job.candidates if c["cid"] not in owned]
    job_persistence.persist(job)
    job.notify_review_changed()
    return sum(1 for sel in owned.values() if sel)


def _record_last_scan():
    try:
        cfg.LAST_SCAN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_scan_seen(mode):
    """Fingerprints the last completed walk of this mode surfaced, or None if
    there's no prior run to compare against (first scan badges nothing)."""
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bucket = data.get(mode) if isinstance(data, dict) else None
    return set(bucket) if isinstance(bucket, list) else None


def _save_scan_seen(mode, fingerprints):
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[mode] = sorted(fingerprints)
    try:
        cfg.SCAN_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.SCAN_SEEN_FILE.with_suffix(cfg.SCAN_SEEN_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cfg.SCAN_SEEN_FILE)
    except OSError:
        pass


def _flag_new_since_last_scan(job, mode):
    """Badge candidates whose album wasn't surfaced by the previous walk, then
    record this walk's set for next time. First-ever run badges nothing (no
    baseline to diff). Skipped on a cancelled scan so a partial run can't
    poison the baseline."""
    # Snapshot under the lock — the scan worker appends candidates to this
    # list and we walk it twice here, which without a snapshot is not safe
    # against a same-instant append. `dismiss_albums` uses the same pattern.
    with job._lock:
        candidates = list(job.candidates)
    seen_now = set()
    fps = {}
    for c in candidates:
        fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
        if fp:
            seen_now.add(fp)
            fps[c["cid"]] = fp
    prev = _load_scan_seen(mode)
    if prev is not None:
        for c in candidates:
            fp = fps.get(c["cid"])
            if fp and fp not in prev:
                c["payload"]["is_new"] = True
    _save_scan_seen(mode, seen_now)


def dismiss_albums(job, artist, scope=hidden_mod.SCOPE_MISSING, gap_only=None):
    """Hide ``artist``'s albums that aren't currently selected, in ``scope``.

    Selection is server-backed (saved as the user ticks), so "hide the rest"
    means: of this artist's candidates, hide the ones whose saved `selected`
    flag is off and keep the ticked ones. Other artists' candidates and their
    selections are never touched — critical now that pagination means most of
    them aren't even on the page that triggered the hide.

    ``gap_only`` narrows the hide to one side of a library review's tab split:
    True drops only Gap Fill candidates, False only fully missing albums, None
    (the default) both. The button only ever shows one tab's rows, so it must
    not silently dismiss the other tab's.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Returns the
    number hidden.
    """
    from qobuz_librarian.web import job_persistence

    # Snapshot + mutate under the lock in one go: a live scan appends candidates
    # from the worker thread, so reading job.candidates and replacing it in
    # separate steps could drop a concurrently-added album.
    with job._lock:
        to_hide = [c for c in job.candidates
                   if c.get("artist") == artist and not c.get("selected")
                   and (gap_only is None or is_gap_candidate(c) == gap_only)]
        if not to_hide:
            return 0
        drop = {c["cid"] for c in to_hide}
        # Only this artist's unselected candidates leave; every other
        # candidate (and its saved selection) is preserved untouched.
        job.candidates = [c for c in job.candidates if c["cid"] not in drop]
        specs = [(c.get("artist"), c.get("title"),
                  (c.get("payload") or {}).get("year")) for c in to_hide]
    # File write + persist outside the lock — neither needs it, and hide does
    # disk I/O that shouldn't stall the scan thread's next add_candidate.
    hidden_mod.hide(scope, specs)
    job_persistence.persist(job)
    return len(to_hide)


# ── Scans ─────────────────────────────────────────────────────────────────────


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: find one artist's gaps. Runs in a pool thread (its own HTTP
    session); returns plain data so the caller adds candidates serially —
    keeping job.candidates single-writer. Also returns the artist's id and its
    lossless catalog ids so the caller can seed the new-release baseline (the
    discography is already fetched here)."""
    result = find_missing_for_artist(
        artist_dir.name, token=token,
        opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        artist_dir=artist_dir, hidden=hidden,
        single_store=hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None,
        want_missing=not partial_only)
    artist_id = str(result.artist_id) if result.artist_id else None
    # None signals "don't seed a baseline" — a transient short-page fetch isn't
    # the whole discography, so seeding it would later dump the dropped albums
    # as "new". The gaps are still surfaced this scan; the artist just stays
    # un-baselined until a complete fetch.
    catalog_ids = None if result.catalog_incomplete else [
        str(a["id"]) for a in result.catalog
        if is_lossless_album(a) and a.get("id") is not None]
    return artist_dir.name, result.artist_name, result.gaps, artist_id, catalog_ids


_CHECKPOINT_EVERY = 15  # artists between progress saves (resume granularity)
# Seconds between live-status refreshes during the whole-library repair sweep
# (see scan_repairs). A clean library logs nothing for minutes (only problems
# print), which would read as a hang — so a worker refreshes the in-place
# progress line this often, keeping the scan visibly alive. Short, because it
# rewrites one line rather than appending to the log.
_REPAIR_HEARTBEAT_SECS = 2


def scan_library(job, token, partial_only=False, force_full=False):
    clear_scan_caches()
    # Drop the Various-Artists folder: it has no single Qobuz artist catalog to
    # diff against, so a gap scan can only mis-resolve it. The upgrade/downsample
    # scans already filter it — this keeps the missing/partial scan consistent.
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    kind = "partial" if partial_only else "missing"
    # Resume an interrupted scan of this kind: skip the artists already done and
    # restore the albums they turned up, so we continue rather than restart.
    cp = scan_checkpoint.load(kind)
    resuming = cp is not None
    checkpoint_artists = dict(cp.get("artists") or {}) if resuming else {}
    current_artist_names = {ad.name for ad in artists}
    if resuming:
        scanned = set()
        baseline_seen = {}
        for name in set(cp["scanned"]):
            if name not in current_artist_names:
                continue
            saved = checkpoint_artists.get(name)
            if not isinstance(saved, dict) or saved.get("catalog_ids") is None:
                continue
            scanned.add(name)
            artist_id = saved.get("artist_id") or ""
            if artist_id:
                baseline_seen[str(artist_id)] = list(saved.get("catalog_ids") or [])
    else:
        scanned = set()
        baseline_seen = {}
    total = 0
    # Snapshot the dismissed-album memory before restoring the checkpoint so
    # albums the user dismissed since the interruption are not re-added, and
    # so the parallel workers below see the same consistent view.
    hidden = hidden_mod.load()
    hidden_sig = library_scan_state.hidden_signature(
        hidden, hidden_mod.SCOPE_MISSING)
    previous_scan = library_scan_state.kind_state(kind)
    cheap_refresh = (
        not force_full
        and not resuming
        and previous_scan.get("complete")
        and previous_scan.get("hidden_signature", "") == hidden_sig
    )
    # The two refreshes and the fingerprint pass below run before the main
    # artist loop, and on a first scan of a large library each takes real
    # minutes — without progress ticks the job sits on "Waiting for output"
    # looking hung the whole time.
    downsample_refresh_started_at = time.time()
    log.info(f"Reading albums from {plural(len(artists), 'artist folder')} on disk…")
    downsample_refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        persist=False,
        skip_unchanged=cheap_refresh,
        on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
            "Reading albums on disk", done_i, total_i, ad.name, unit="artist"),
    )
    upgrade_refresh = None
    upgrade_refresh_started_at = None
    if not job.cancel_requested and cfg.UPGRADE_SCAN_ENABLED:
        from qobuz_librarian.quality.decision import load_capped
        from qobuz_librarian.web.jobs import pool_initializer_kwargs
        upgrade_refresh_started_at = time.time()
        log.info("Comparing owned albums against the editions Qobuz can serve…")
        upgrade_refresh = upgrade_state.refresh_for_artists(
            artists,
            token=token,
            args=build_args(),
            capped=load_capped(),
            hidden=hidden,
            cancel_check=lambda: bool(job.cancel_requested),
            workers=max(1, int(cfg.ARTIST_SCAN_WORKERS)),
            pool_kwargs=pool_initializer_kwargs(),
            skip_unchanged=cheap_refresh,
            persist=False,
            on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
                "Checking upgrade quality", done_i, total_i, ad.name, unit="artist"),
        )
    elif not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
    target = "Gap Fill candidates in owned albums" if partial_only else "missing albums"
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target}")
    fingerprints = {}
    for _i, _ad in enumerate(artists, 1):
        fingerprints[_ad.name] = artist_fingerprint(_ad)
        if _i % 25 == 0 or _i == len(artists):
            job.push_progress("Fingerprinting artist folders", _i, len(artists),
                              _ad.name, unit="folder")
    previous_artists = (previous_scan.get("artists") or {}) if cheap_refresh else {}
    state_artists: dict[str, dict] = {}
    if resuming:
        restored_by_artist: dict[str, list[dict]] = {}
        for c in cp["candidates"]:
            artist_key = (c.get("payload") or {}).get("_artist_dir") or c.get("artist")
            if artist_key not in scanned:
                continue
            if hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING,
                                    c.get("artist"), c.get("title"), hidden):
                continue
            _readd_candidate(job, c)
            total += 1
            if artist_key:
                restored_by_artist.setdefault(artist_key, []).append(c)
        for name in scanned:
            saved = checkpoint_artists.get(name)
            if not isinstance(saved, dict):
                continue
            catalog_ids = saved.get("catalog_ids")
            if catalog_ids is None:
                continue
            candidates = [
                c for c in saved.get("candidates", [])
                if not hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING,
                    c.get("artist"),
                    c.get("title"),
                    hidden,
                )
            ]
            state_artists[name] = {
                "fingerprint": saved.get("fingerprint") or fingerprints.get(name, ""),
                "candidates": candidates or restored_by_artist.get(name, []),
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(catalog_ids or []),
            }
        log.info(f"Resuming. {len(scanned)} artist(s) already scanned, "
                 f"{plural(total, 'album')} found so far.")
    todo = []
    n = len(artists)
    done = len(scanned)
    reused = 0
    scan_errors = 0
    for artist_dir in artists:
        if artist_dir.name in scanned:
            continue
        saved = previous_artists.get(artist_dir.name)
        saved_catalog_ids = saved.get("catalog_ids") if saved else None
        if (
            saved
            and saved_catalog_ids is not None
            and saved.get("fingerprint") == fingerprints.get(artist_dir.name)
            and (saved.get("artist_id") or not saved.get("candidates"))
        ):
            candidates = [
                c for c in saved.get("candidates", [])
                if not hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING,
                    c.get("artist"),
                    c.get("title"),
                    hidden,
                )
            ]
            for c in candidates:
                _readd_candidate(job, c)
                total += 1
            scanned.add(artist_dir.name)
            done += 1
            reused += 1
            if saved.get("artist_id"):
                baseline_seen[str(saved["artist_id"])] = list(saved_catalog_ids or [])
            state_artists[artist_dir.name] = {
                "fingerprint": fingerprints.get(artist_dir.name, ""),
                "candidates": candidates,
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(saved_catalog_ids or []),
            }
            hit = ({"artist": artist_dir.name, "albums": len(candidates)}
                   if candidates else None)
            job.push_progress("Scanning library", done, n, artist_dir.name,
                              found=total, hit=hit, unit="artist")
        else:
            todo.append(artist_dir)
    if reused:
        log.info(f"  Reused {plural(reused, 'unchanged artist')} from the saved scan.")
    since_save = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Resolve/scan artists in parallel (each worker has its own HTTP session),
    # but collect results and write candidates on this one thread so the
    # candidate list and progress stay single-writer.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_library_artist, ad, token, partial_only,
                             hidden): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled. Stopping scan.")
                break
            done += 1
            try:
                name, artist_name, gaps, artist_id, catalog_ids = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup —
                # cancel the rest and fail the scan rather than silently report a
                # partial library as the full picture. The checkpoint stays, so
                # the scan resumes once the token/network is back.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                scan_errors += 1
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Scanning library", done, n, futures[fut].name,
                                  found=total, unit="artist")
                continue
            scanned.add(name)
            if artist_id and catalog_ids is not None:
                baseline_seen[artist_id] = catalog_ids
            artist_candidates = []
            for gap in gaps:
                # Library is a discovery list — leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                spec = _gap_candidate_spec(
                    gap, artist_name or name, selected=False, artist_key=name)
                if _add_candidate_spec(job, spec) is not None:
                    artist_candidates.append(spec)
                    total += 1
            if catalog_ids is not None:
                state_artists[name] = {
                    "fingerprint": fingerprints.get(name, ""),
                    "candidates": artist_candidates,
                    "artist_id": artist_id or "",
                    "catalog_ids": list(catalog_ids or []),
                }
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": len(gaps)}
                   if gaps else None)
            job.push_progress("Scanning library", done, n, artist_name or name,
                              found=total, hit=hit, unit="artist")
            if gaps:
                tail = "with Gap Fill candidates" if partial_only else "to fill"
                log.info(f"  {artist_name} — {plural(len(gaps), 'album')} {tail}")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save(
                    kind, scanned, job.candidates, baseline_seen, state_artists)
    # Reached here only without an AuthLost/outage abort (that re-raises out
    # above, leaving the checkpoint for resume and not seeding the baseline).
    flush_resolve_cache()
    if job.cancel_requested:
        # Deliberate stop — discard this kind's progress so it isn't auto-resumed.
        scan_checkpoint.clear(kind)
    else:
        # Only a reached-all-artists crawl stamps "last scanned" or seeds the
        # new-release baseline. A candidate cap means the review list is partial,
        # but the catalog crawl can still be complete.
        catalog_complete = (
            len(state_artists) == len(artists)
            and len(scanned) == len(artists)
            and scan_errors == 0
        )
        library_complete = (
            catalog_complete
            and not job.candidate_cap_hit
        )
        library_scan_state.save_kind(
            kind,
            artists=state_artists,
            complete=library_complete,
            hidden_signature=hidden_sig,
        )
        if library_complete:
            if downsample_refresh.complete:
                downsample_state.save(
                    downsample_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=downsample_refresh_started_at,
                )
                review_badges.set_ready(
                    "downsample", _surface_has_candidates("downsample"))
            if upgrade_refresh is not None and upgrade_refresh.complete:
                upgrade_state.save(
                    upgrade_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=upgrade_refresh_started_at,
                )
                review_badges.set_ready(
                    "upgrade", _surface_has_candidates("upgrade"))
        if catalog_complete:
            _record_last_scan()
            _flag_new_since_last_scan(job, kind)
            # The crawl reached every artist cleanly — establish the new-release
            # baseline from the catalog snapshot (only the first time; the daily
            # check keeps it fresh after), and clear this kind's checkpoint.
            if not new_releases_mod.is_baseline_complete():
                new_releases_mod.seed_baseline(baseline_seen)
            scan_checkpoint.clear(kind)
        elif scanned or job.candidates or baseline_seen:
            scan_checkpoint.save(
                kind, scanned, job.candidates, baseline_seen, state_artists)
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    elif partial_only:
        job.summary = (f"{plural(total, 'album')} with Gap Fill candidates across the library."
                       + _cap_note(job)
                       if total else "No Gap Fill candidates found in your owned albums.")
    else:
        job.summary = (f"{plural(total, 'missing album')} across the library."
                       + _cap_note(job)
                       if total else
                       "No missing albums found for artists in your library.")
    # Artists that errored or came back with a short catalog page aren't in
    # state_artists; the checkpoint stays for them and the last-scan stamp is
    # withheld. Say so — otherwise the summary reads as a clean, definitive
    # total and the resume prompt that follows looks unexplained.
    unchecked = len(artists) - len(state_artists)
    if not job.cancel_requested and unchecked > 0:
        job.summary += (f" {plural(unchecked, 'artist')} couldn't be checked; "
                        "scan again to resume from where it left off.")
    log.info(job.summary)


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden — flagged as new
    for review, but left un-ticked so one click can't queue the whole list.
    Cheap (one catalog call per artist, no track fetches), so it's the quick
    "what's new" pass rather than the full gap scan."""
    clear_scan_caches()
    # Same VA exclusion as scan_library: the Various-Artists folder has no single
    # Qobuz catalog, so it can't yield meaningful "new releases".
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    # If the catalog fetch limit has grown since the baseline was captured, the
    # old baseline is missing everything past the previous cap — a plain diff
    # would dump that whole back-slice as "new". Re-baseline this run instead
    # (record the wider snapshot, surface nothing); real diffs resume next run. A
    # pre-tracking baseline (limit unknown) re-baselines once, then gets stamped.
    cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
    prev_limit = state.get("baseline_limit")
    rebaseline = prev_limit is None or cur_limit > int(prev_limit)
    hidden = hidden_mod.load()
    single_store = hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)
    log.info(f"Checking {plural(len(artists), 'artist')} for new releases…")
    total = 0
    done = 0
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # This run's reached artists; merged over the prior baseline at the end (so a
    # run where some/all artists errored can't wipe their baselines and re-surface
    # everything — only artists actually reached get their snapshot refreshed).
    current_seen = {}
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(find_new_releases_for_artist, ad.name, token=token,
                             opts=opts, seen_by_id=seen, hidden=hidden,
                             single_store=single_store, artist_dir=ad,
                             baseline_only=rebaseline): ad
                   for ad in artists}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled. Stopping check.")
                break
            done += 1
            try:
                result = fut.result()
            except (AuthLost, QobuzUnavailable):
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Checking for new releases", done, n,
                                  futures[fut].name, found=total, unit="artist")
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
            for gap in result.new_gaps:
                # Leave new releases UN-ticked, like the library gap list: a
                # review is for picking, and one tap must never queue the whole
                # list of rips. The "new" badge still flags them for the eye.
                _add_gap_candidate(job, gap, result.artist_name,
                                   selected=False, is_new=True)
                total += 1
            hit = ({"artist": result.artist_name, "albums": len(result.new_gaps)}
                   if result.new_gaps else None)
            job.push_progress("Checking for new releases", done, n,
                              result.artist_name or futures[fut].name,
                              found=total, hit=hit, unit="artist")
            if result.new_gaps:
                log.info(f"  {result.artist_name} — "
                         f"{plural(len(result.new_gaps), 'new release')}")
    flush_resolve_cache()
    if not job.cancel_requested:
        # UNION each reached artist's snapshot into the prior baseline rather than
        # replacing it. A catalog bigger than the fetch cap comes back as a
        # different slice each run, so a replace would let an id that rotated out
        # of this run's window re-surface as "new" next time, so the count would
        # never converge. Unioning only ever grows an artist's baseline, so the
        # diff settles. An artist that errored this run keeps
        # its old entry; a clean check establishes the baseline like a library scan.
        merged = dict(seen)
        for aid, ids in current_seen.items():
            merged[aid] = sorted(set(merged.get(aid, [])) | set(ids))
        new_releases_mod.mark_run(merged, complete=True, baseline_limit=cur_limit)
    if job.cancel_requested:
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        job.summary = ("Stopped early. Partial check, "
                       f"{plural(total, 'new release')} found so far.")
        log.info(job.summary)
        return
    if rebaseline and seen:
        job.summary = ("Catalogue limit changed. Recorded a fresh baseline. "
                       "Future checks will flag new releases.")
        log.info("Re-baselined after a catalogue-limit change; nothing surfaced.")
    elif total:
        job.summary = f"{plural(total, 'new release')} found across the library."
        log.info(f"Done. {plural(total, 'new release')} across the library.")
    elif not seen:
        job.summary = ("First check complete. Recorded the current Qobuz "
                       "catalogue baseline. Future checks will flag new releases.")
        log.info("Baseline recorded. Future checks will flag new releases.")
    else:
        job.summary = "No new releases added to your saved baseline."
        log.info("No new releases added to the saved baseline.")


# ── Execute ───────────────────────────────────────────────────────────────────

def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.web.jobs import staging_lock

    # The web worker runs jobs back-to-back; a directory listing cached by
    # a previous job would otherwise be reused even though folders may
    # have moved since.
    clear_scan_caches()
    args = build_args()
    _benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
               "user_skipped", "lossy_only", "no_tracks", "skipped_has_extras",
               "cancelled"}
    ok = 0
    partial = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} — {cand['title']}"
        log.info(label)
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Downloading albums", i, len(chosen),
                          f"{cand.get('artist','')} — {cand['title']}", unit="album")
        try:
            full = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        try:
            with staging_lock():
                result = process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        if result and result.get("imported") and result.get("n_ok", 0) > 0:
            _refresh_after_local_album_change(
                full,
                result,
                fallback_artist=cand.get("artist"),
                downsample=True,
            )
            # The album is on disk now — any OTHER parked library review still
            # offering it is stale.
            prune_library_review_candidates(full)
            # A partial (some tracks landed, some failed) isn't a full download —
            # count it apart so the summary doesn't claim it finished.
            if result.get("n_fail", 0) > 0:
                partial += 1
            else:
                ok += 1
        elif not (result and result.get("result") in _benign):
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. {ok} downloaded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    parts = [f"{ok}/{plural(len(chosen), 'album')} downloaded and imported"]
    if partial:
        parts.append(f"{plural(partial, 'album')} only partly (some tracks failed)")
    job.summary = "Finished. " + ", ".join(parts) + "."
    log.info(f"Finished. {ok}/{plural(len(chosen), 'album')} downloaded and imported.")
    if partial:
        log.info(f"  {plural(partial, 'album')} downloaded only partly "
                 f"(some tracks failed); see the log.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} didn't finish; see the log."


# ── Upgrade flow ──────────────────────────────────────────────────────────────

def scan_upgrades(job, token):
    """Scan the library for albums Qobuz can serve at higher quality."""
    from qobuz_librarian.quality.decision import load_capped

    if not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
        job.summary = "Upgrade scanning is turned off."
        log.info(job.summary)
        return
    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    args = build_args()
    capped = load_capped()
    # Upgrades the user dismissed ("I'm happy with my copy") — independent of
    # the auto-`capped` memory and of the missing-album hides.
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for quality upgrades")
    total = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    from qobuz_librarian.web.jobs import pool_initializer_kwargs

    def _on_artist(ad, specs, error, done, n):
        nonlocal total
        name = ad.name
        if isinstance(error, (AuthLost, QobuzUnavailable)):
            raise error
        if error is not None:
            log.info(f"    skipped {name}: {error}")
            job.push_progress("Scanning for upgrades", done, n, name,
                              found=total, unit="artist")
            return
        added = 0
        current_hidden = hidden_mod.load()
        for spec in specs:
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_UPGRADE,
                    spec.get("artist") or name,
                    spec.get("title"),
                    current_hidden):
                continue
            # Unticked by default — like the gap scan, one click shouldn't
            # re-rip hundreds of albums nobody reviewed.
            job.add_candidate(
                kind="upgrade",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or name,
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for upgrades", done, n, name,
                          found=total, hit=hit, unit="artist")
        if added:
            log.info(f"  {name} — {plural(added, 'album')} to upgrade")

    refresh = upgrade_state.refresh_for_artists(
        artists,
        token=token,
        args=args,
        capped=capped,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
        workers=workers,
        pool_kwargs=pool_initializer_kwargs(),
    )
    if not job.cancel_requested and refresh.complete:
        review_badges.set_ready("upgrade", _surface_has_candidates("upgrade"))
    if job.cancel_requested or not refresh.complete:
        log.info("Cancelled. Stopping scan.")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "upgrade")
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'upgradeable album')} Qobuz can serve "
                       "at higher quality." + _cap_note(job) if total else
                       "No upgrades; every album is already at the best quality "
                       "Qobuz offers.")
    log.info(job.summary)


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.modes.upgrade import BENIGN_UPGRADE_RESULTS
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure — the album just didn't need (or couldn't
    # safely take) an upgrade. Shared with the CLI upgrade walk; a backup-failed
    # abort is deliberately excluded, so it counts as the real failure it is.
    _skip = BENIGN_UPGRADE_RESULTS
    ok = 0
    kept = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} — "
                 f"{cand.get('title') or '?'}")
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Upgrading albums", i, len(chosen),
                          f"{cand.get('artist','')} — {cand.get('title') or '?'}", unit="album")
        try:
            album = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        if not album:
            log.info(f"  album {album_id} is no longer on Qobuz; skipping.")
            failed += 1
            continue
        try:
            with staging_lock():
                result = process_album(album, args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        _res = (result or {}).get("result")
        if result and result.get("upgrade_unverified"):
            # Imported, but the rebuilt folder couldn't be verified as complete
            # as the original, so the backup was kept. Not a clean upgrade and
            # not a failure — count it apart so the tally stays honest.
            kept += 1
        elif result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
            verdict = result.get("quality_verdict")
            if verdict and verdict["under"] and not verdict["recovered"]:
                from qobuz_librarian.quality.decision import mark_album_capped
                mark_album_capped(album.get("id"), album, {
                    "n_below": verdict["n_below"],
                    "n_at": 0,
                    "n_above": 0,
                })
                log.info(f"  upgrade incomplete: {verdict['n_below']} "
                         f"track(s) still below target after retry. Marked capped.")
            _refresh_after_local_album_change(
                album,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # The verified replace refreshed the whole album, so a parked
            # library review's candidates for it (incl. Gap Fill) are stale.
            prune_library_review_candidates(album)
        elif result and _res in (_skip - {"cancelled", "dry_run"}):
            _refresh_after_local_album_change(
                album,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
        elif _res not in _skip:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. {ok} upgraded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    msg = f"Finished. Upgraded {ok}/{plural(len(chosen), 'album')}."
    if kept:
        msg += (f" {kept} kept the original (upgrade couldn't be verified "
                f"complete; backup retained).")
    job.summary = msg
    log.info(msg)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be upgraded; see the log."


# ── Downsample flow ─────────────────────────────────────────────────────────────

def scan_downsamples(job):
    """Scan the library for FLACs stored above CD rate.

    Local only — the answer comes off disk, so unlike the upgrade scan there's
    no Qobuz lookup and no token. Serial (the per-file read is fast and disk-
    bound; fanning out would just thrash the spindle) with a cancel check and
    per-artist progress.
    """
    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for hi-res files to downsample")
    total = 0

    def _on_artist(ad, cands, error, done, n):
        nonlocal total
        name = ad.name
        if error is not None:
            log.info(f"    skipped {name}: {error}")
            job.push_progress("Scanning for hi-res files", done, n, name,
                              found=total, unit="artist")
            return
        added = 0
        current_hidden = hidden_mod.load()
        for c in cands:
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_DOWNSAMPLE, c.artist, c.title, current_hidden):
                continue
            # Unticked by default — a downsample is irreversible, so nothing is
            # shrunk without an explicit per-album tick.
            job.add_candidate(
                kind="downsample",
                title=c.title,
                artist=name,
                detail=c.detail,
                payload={"album_dir": str(c.album_dir), "est_saving": c.est_saving},
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for hi-res files", done, n, name,
                          found=total, hit=hit, unit="artist")
        if added:
            log.info(f"  {name} — {plural(added, 'album')} above CD rate")

    refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
    )
    if not job.cancel_requested and refresh.complete:
        review_badges.set_ready("downsample", _surface_has_candidates("downsample"))
    if job.cancel_requested or not refresh.complete:
        log.info("Cancelled. Stopping scan.")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "downsample")
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'album')} stored above CD rate."
                       + _cap_note(job)
                       if total else
                       "No hi-res files; every album is already at CD rate or lower.")
    log.info(job.summary)


def execute_downsamples(job, chosen, token=None, args=None):
    """Shrink the chosen albums' hi-res FLACs to CD rate, in place.

    Each file is decode-verified before it overwrites the original (in
    resample_one), so a bad encode can't destroy a master that has no
    re-download fallback.
    """
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
    from qobuz_librarian.quality.decision import mark_local_album_capped
    from qobuz_librarian.web.jobs import staging_lock

    if not HAVE_DOWNSAMPLE:
        job.error = "Downsampling isn't available on this server."
        return
    shrunk = 0
    total_saved = 0
    total_errors = 0
    skipped = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        raw_album_dir = (cand.get("payload") or {}).get("album_dir")
        if not raw_album_dir:
            log.info("  skipped: saved candidate is missing its folder path")
            skipped += 1
            continue
        album_dir = Path(raw_album_dir)
        title = cand.get("title") or album_dir.name
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist', '')} — {title}")
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Downsampling albums", i, len(chosen),
                          f"{cand.get('artist', '')} — {title}", unit="album")
        if not album_dir.is_dir():
            log.info("  skipped: folder no longer exists")
            skipped += 1
            if album_dir.parent.is_dir():
                _refresh_downsample_artist_state(album_dir.parent)
            else:
                downsample_state.remove_artist(album_dir.parent.name)
                review_badges.set_ready(
                    "downsample", _surface_has_candidates("downsample"))
            continue
        try:
            with staging_lock():
                res = downsample_dir(album_dir, verbose=True,
                                     base_dir=album_dir, log=log.info,
                                     keep_originals=cfg.DOWNSAMPLE_KEEP_ORIGINALS)
        except Exception as e:
            log.info(f"  failed: {e}")
            total_errors += 1
            continue
        if res.get("resampled"):
            shrunk += 1
            mark_local_album_capped(album_dir)
            if token:
                try:
                    _refresh_upgrade_artist_state(
                        album_dir.parent, token, args=args or build_args())
                except (AuthLost, QobuzUnavailable) as exc:
                    log.info(
                        f"  upgrade view refresh skipped after downsample: {exc}")
        _refresh_downsample_artist_state(album_dir.parent)
        total_saved += res.get("saved_bytes", 0)
        total_errors += res.get("errors", 0)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. Downsampled {plural(shrunk, 'album')} "
                       f"({format_size(total_saved)} reclaimed), "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    summary = (f"Finished. Downsampled {plural(shrunk, 'album')}, "
               f"reclaimed {format_size(total_saved)}.")
    if skipped:
        summary += f" {plural(skipped, 'album')} skipped (no longer on disk)."
    job.summary = summary
    log.info(summary)
    if total_errors:
        job.error = (f"{plural(total_errors, 'file')} couldn't be downsampled "
                     "(left unchanged); see the log.")


# ── Repair flow ───────────────────────────────────────────────────────────────

def _repair_album_outcome(album_dir, name, token):
    """Scan one album into an outcome dict: counts, review-candidate specs, and
    any log lines to emit. AuthLost / QobuzUnavailable propagate (they stop the
    sweep); any other scan error is recorded as a failed album."""
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs
    out = {"verified_ok": 0, "unverified": 0, "failed": 0, "specs": [],
           "warns": []}
    try:
        scan = scan_dir_for_isrc_repairs(album_dir, token, deep=True)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        out["warns"].append(f"    skipped {album_dir.name}: {e}")
        out["failed"] = 1
        return out
    out["verified_ok"] = scan["verified_ok"]
    out["unverified"] = scan.get("unverified", 0)
    truncated = scan["verified_truncated"]
    if truncated:
        out["specs"].append({
            "kind": "repair", "title": album_dir.name, "artist": name,
            "detail": f"{plural(len(truncated), 'truncated track')}",
            "payload": {"album_dir": str(album_dir), "artist_name": name,
                        "verified_truncated": truncated}})
    # Damaged files with no readable ISRC can't be surgically refilled — offer a
    # whole-album re-download instead (the user confirms it in review).
    suspicious = [e for e in scan.get("no_isrc_tag", []) if e.get("diagnostic")]
    if suspicious:
        matched = find_qobuz_album_for_dir(album_dir, name, token)
        if matched and matched.get("id"):
            m_title = matched.get("title") or album_dir.name
            m_year = album_year(matched) or "?"
            out["specs"].append({
                "kind": "redownload", "title": album_dir.name, "artist": name,
                "detail": (f"{plural(len(suspicious), 'damaged file')} can't be "
                           f"verified by ID. Re-download the whole album fresh "
                           f"as “{m_title}” ({m_year})"),
                "payload": {"album_dir": str(album_dir), "artist_name": name,
                            "album_id": matched.get("id"),
                            "matched_title": m_title}})
        else:
            for e in suspicious:
                out["warns"].append(
                    f"    ⚠ {album_dir.name} — {e.get('title') or '?'}: "
                    f"{e['diagnostic']}; couldn't match this folder to a Qobuz "
                    "album to re-download. Check by hand.")
    return out


def _scan_repair_artist(artist_dir, token, job, beat=None):
    """Scan one artist's albums for damaged FLACs — runs on a pool worker.

    Returns ``(name, agg)``; ``agg`` carries per-artist counts and a list of
    review-candidate specs the caller adds on the single writer thread, so the
    candidate list and checkpoint stay single-writer (mirroring the library
    scan). Every album is decode-tested fresh each run so on-disk corruption is
    always caught; the per-track Qobuz lookups are what's cached, so a re-scan
    only re-reads files rather than re-crawling Qobuz. Bails between albums on
    cancel; AuthLost / QobuzUnavailable propagate so the caller can stop the
    sweep."""
    name = artist_dir.name
    agg = {"verified_ok": 0, "unverified": 0, "failed": 0, "checked": 0,
           "specs": []}
    for album_dir in list_artist_album_dirs(artist_dir):
        if job.cancel_requested:
            break
        outcome = _repair_album_outcome(album_dir, name, token)
        agg["verified_ok"] += outcome.get("verified_ok", 0)
        agg["unverified"] += outcome.get("unverified", 0)
        agg["failed"] += outcome.get("failed", 0)
        agg["checked"] += 1
        agg["specs"].extend(outcome.get("specs", []))
        for w in outcome.get("warns", []):
            log.info(w)
        if beat is not None:
            _emit_repair_heartbeat(beat, job, name)
    return name, agg


def _repair_item(artist, albums, flagged):
    """One consistent live-status line for the whole-library repair sweep.

    Every progress push during the sweep — the per-album heartbeat, the
    per-artist completion tick, and the failure tick — renders through this so
    the job page's detail line updates *in place* (the artist and the counts
    climbing) instead of structurally flip-flopping between an artist-name form
    and a bare-tally form, which reads as flicker. ``artist`` is whichever one a
    worker is currently grinding on (carried in ``beat['current']``); it is
    empty only in the opening instant before the first heartbeat fires, where a
    neutral label stands in."""
    if not artist and not albums:
        return "Starting…"
    who = artist or "your library"
    return f"{who} · {albums:,} albums checked · {flagged:,} flagged"


def _emit_repair_heartbeat(beat, job, artist_name):
    """Refresh the live progress line from whichever worker crosses the interval,
    so it keeps ticking even while every worker is deep inside one large artist
    and no future has completed — otherwise a long stretch with no completed
    artist looks like a hang. Pushed through the progress channel, not the log,
    so the activity log stays a list of flagged albums rather than a scroll of
    heartbeats. Counts are shared under ``beat['lock']``."""
    with beat["lock"]:
        beat["albums"] += 1
        if time.time() - beat["last"] < _REPAIR_HEARTBEAT_SECS:
            return
        beat["last"] = time.time()
        # Advance the displayed artist only when the throttled beat actually
        # fires, so the detail line names a stable artist for a calm interval
        # (rather than hopping every time a worker finishes a small one), while
        # the completion ticks in between keep the bar and counts climbing.
        beat["current"] = artist_name
        albums, artists, flagged, n = (beat["albums"], beat["artists"],
                                       beat["flagged"], beat["n"])
        item = _repair_item(artist_name, albums, flagged)
    job.push_progress("Checking for damaged files", artists, n, item,
                      found=flagged, unit="artist")


def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs (fanned out across
    ARTIST_SCAN_WORKERS; see _scan_repair_artist for the per-artist work)."""
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        _set_empty_library_summary(job)
        return
    # Resume an interrupted sweep: skip the artists already checked and restore
    # the damaged albums they turned up. A repair scan is one Qobuz call per
    # track and runs for hours on a big library, so a container restart or power
    # loss mid-sweep must continue rather than re-check everything from the top.
    cp = scan_checkpoint.load("repair")
    scanned = set(cp["scanned"]) if cp else set()
    total = 0
    n_verified = 0      # ISRC'd FLACs that actually decoded clean this run
    n_unverified = 0    # couldn't decode-check (flac tool absent)
    n_failed = 0        # albums that errored mid-scan (surfaced, not hidden)
    if cp:
        for c in cp["candidates"]:
            _readd_candidate(job, c)
            total += 1
        log.info(f"Resuming. {len(scanned)} artist(s) already checked, "
                 f"{plural(total, 'album')} flagged so far.")
    log.info(f"Scanning {plural(len(artists), 'artist')} for damaged files. "
             "Only problems are listed below; expect long quiet stretches. "
             "Slow on a big library.")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    since_save = 0
    # Shared heartbeat state: workers bump it per album and one logs the periodic
    # line when due, so progress keeps showing even while every worker is deep in
    # one large artist and no future has completed (see _emit_repair_heartbeat).
    beat = {"lock": threading.Lock(), "albums": 0, "artists": done,
            "flagged": total, "n": n, "last": time.time(), "current": ""}
    # Show the progress bar immediately rather than a blank header until the
    # first artist comes back.
    job.push_progress("Checking for damaged files", done, n,
                      _repair_item("", 0, total), found=total, unit="artist")
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Scan artists in parallel (each worker gets its own HTTP session), but add
    # candidates, advance progress, and write the checkpoint on THIS one thread
    # so they stay single-writer — the same shape the library scan uses. A repair
    # scan makes a Qobuz call per track, so fanning out is what turns a multi-hour
    # sweep into something watchable.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repairscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_repair_artist, ad, token, job, beat): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                job.summary = (f"Stopped early. {plural(total, 'album')} flagged so far."
                               if total else "Stopped before anything was flagged.")
                log.info("Cancelled. Stopping scan.")
                scan_checkpoint.clear("repair")
                return
            done += 1
            try:
                name, agg = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup —
                # stop the sweep rather than report a partial library as whole.
                # The checkpoint stays, so it resumes once auth/network is back.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {futures[fut].name}: {e}")
                n_failed += 1
                with beat["lock"]:
                    beat["artists"] = done
                    albums_seen = beat["albums"]
                    current = beat["current"]
                job.push_progress("Checking for damaged files", done, n,
                                  _repair_item(current, albums_seen, total),
                                  found=total, unit="artist")
                continue
            n_verified += agg["verified_ok"]
            n_unverified += agg["unverified"]
            n_failed += agg["failed"]
            for spec in agg["specs"]:
                job.add_candidate(**spec)
                total += 1
            scanned.add(name)
            with beat["lock"]:
                beat["artists"] = done
                beat["flagged"] = total
                albums_seen = beat["albums"]
                current = beat["current"]
            job.push_progress("Checking for damaged files", done, n,
                              _repair_item(current, albums_seen, total),
                              found=total, unit="artist")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save("repair", scanned, job.candidates, {})
    scan_checkpoint.clear("repair")
    # Honest summary: report what was actually decode-verified, and never claim
    # completeness the scan didn't earn. Surface the un-checkable (no flac tool)
    # and the albums that errored, instead of folding them into a clean total.
    unver = (f" {plural(n_unverified, 'track')} couldn't be decode-checked "
             "(no flac tool)." if n_unverified else "")
    fail = (f" {plural(n_failed, 'album')} couldn't be scanned; re-run to retry."
            if n_failed else "")
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} decode-verified clean."
                       + unver + fail)
    else:
        job.summary = (f"No damaged files found. "
                       f"{plural(n_verified, 'track')} decode-verified intact."
                       + unver + fail)
    log.info(job.summary)


def _redownload_damaged_album(payload, token):
    """Re-fetch a whole album whose damaged file couldn't be ID-verified.

    The folder is moved aside first so beets imports a clean copy instead of
    colliding with the broken files (the --force path can't be used here: it
    needs an interactive deletion confirm the web has no way to answer). If
    the re-download doesn't complete, the original folder is moved back so the
    user is never left worse off.
    """
    import shutil as _shutil

    from qobuz_librarian.library.backup import (
        backup_album_dir,
        restore_upgrade_backup,
    )
    from qobuz_librarian.modes.process import (
        _upgrade_replacement_verified,
        process_album,
    )
    from qobuz_librarian.web.jobs import staging_lock

    log.info("  The damaged file can't be verified by its ID, so the whole "
             "album is being re-downloaded fresh from Qobuz.")
    full = get_album(payload["album_id"], token)
    album_dir = Path(payload["album_dir"])
    backup = backup_album_dir(album_dir) if album_dir.exists() else None
    if album_dir.exists() and backup is None:
        log.info("  Couldn't move the existing folder aside; left this album "
                 "alone. See the log above.")
        return {"imported": False, "n_ok": 0, "result": "backup_failed"}
    try:
        with staging_lock():
            result = process_album(full, build_args(), allow_force=False,
                                   already_confirmed=True, token=token) or {}
    except Exception:
        if backup:
            restore_upgrade_backup(backup, album_dir)
        raise
    imported_ok = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        if imported_ok and _upgrade_replacement_verified(full, album_dir, backup):
            # The rebuild is verifiably at least as complete as the original
            # (track count + playtime) — safe to drop the backup.
            _shutil.rmtree(backup, ignore_errors=True)
        elif imported_ok:
            # Imported, but a decode pass alone doesn't prove the re-rip kept
            # every track — a truncated or short result could be WORSE than the
            # damaged original it replaced. Keep the only copy that may hold
            # more rather than deleting it on the old decode-only gate.
            log.info("  Re-download landed but couldn't be verified as complete "
                     f"as the original; keeping your backup at {backup}.")
        else:
            log.info("  Re-download didn't complete. Restoring the original "
                     "album folder.")
            restore_upgrade_backup(backup, album_dir)
    return result


def execute_repairs(job, chosen, token):
    """Refill ISRC-verified truncated tracks, or re-download whole albums
    whose damage couldn't be ID-verified — depending on each candidate."""
    from qobuz_librarian.modes.repair import repair_album_dir
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    fixed = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        p = cand["payload"]
        # Pin the progress card to album-level scope so the inner per-album
        # phases (download / import / downsample) read "album i / N" instead of
        # resetting it to 1 / 1 — the card now reflects the whole batch.
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Repairing damaged albums", i, len(chosen),
                          f"{p['artist_name']} — {cand['title']}", unit="album")
        log.info(f"[{i}/{len(chosen)}] {p['artist_name']} — {cand['title']}")
        try:
            if cand.get("kind") == "redownload":
                # _redownload_damaged_album takes the staging lock itself.
                result = _redownload_damaged_album(p, token)
            else:
                with staging_lock():
                    result = repair_album_dir(Path(p["album_dir"]),
                                              p["verified_truncated"],
                                              p["artist_name"], args, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        # Each chosen album was flagged as damaged, so anything that didn't end
        # up downloaded-and-imported is a real failure.
        if result and result.get("n_ok", 0) > 0 and result.get("imported"):
            fixed += 1
            _refresh_after_local_album_change(
                None,
                result,
                fallback_artist=p.get("artist_name"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
        else:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. {fixed} repaired, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    job.summary = f"Finished. Repaired {fixed}/{plural(len(chosen), 'album')}."
    log.info(job.summary)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired; see the log."


def run_lyric_retry(job):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    from qobuz_librarian.integrations.lyrics import (
        _refresh_lyric_retry,
        load_lyric_retry,
        lyric_fetch,
        save_lyric_retry,
    )

    paths = load_lyric_retry()
    if not paths:
        job.summary = "No tracks were queued for lyric retry."
        log.info(job.summary)
        return

    if not lyric_fetch.AVAILABLE:
        job.summary = ("The syncedlyrics library isn't installed; manifest "
                       "preserved for a later retry.")
        log.info(job.summary)
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{dropped} queued path(s) no longer on disk; skipping.")
    if not existing:
        save_lyric_retry([])
        job.summary = "All queued files are gone from disk; manifest cleared."
        log.info(job.summary)
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    # Hold the staging lock: fetch_for_paths rewrites library FLACs in place, so
    # it must not run concurrently with the scan-lane downsample/repair/upgrade
    # work that mutates the same files (the documented file-mutation mutex).
    from qobuz_librarian.web.jobs import staging_lock
    try:
        with staging_lock():
            lyric_fetch.fetch_for_paths(
                existing, log=log,
                providers=cfg.LYRICS_PROVIDERS or None,
                lyrics_format=cfg.LYRICS_FORMAT,
                state_path=cfg.LYRIC_FETCH_STATE_FILE,
                should_stop=lambda: job.cancel_requested,
            )
    except Exception as e:
        job.error = f"Lyric retry failed: {e}; manifest preserved."
        job.summary = "Lyric retry failed. Manifest preserved, will retry next time."
        log.info(job.error)
        return

    _refresh_lyric_retry(existing)
    remaining = load_lyric_retry()
    resolved = len(existing) - len(remaining)
    if job.cancel_requested:
        job.summary = (f"Stopped. Resolved {resolved}, "
                       f"{plural(len(remaining), 'track')} still queued for retry.")
    elif remaining:
        job.summary = (f"Resolved {resolved}. {plural(len(remaining), 'track')} "
                       "still unresolved, will retry next time.")
    else:
        job.summary = f"All {plural(len(existing), 'retried track')} resolved."
    log.info(job.summary)


def run_library_lyrics(job, *, rescan=False, synced_only=False):
    """Fetch lyrics for every library track that's missing them."""
    from qobuz_librarian.library.lyrics import HAVE_LYRICS
    from qobuz_librarian.library.lyrics import run_library_lyrics as engine

    if not HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available; the syncedlyrics library isn't installed."
        log.info(job.summary)
        return

    log.info(f"Fetching lyrics across the library (writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track (ignoring saved state).")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    from qobuz_librarian.web.jobs import staging_lock
    with staging_lock():
        res = engine(rescan=rescan, synced_only=synced_only,
                     should_stop=lambda: job.cancel_requested, log=log)

    total = res.get("total", 0)
    if not total:
        job.summary = "No FLAC files found in the library."
        log.info(job.summary)
        return
    if res.get("stopped"):
        job.summary = f"Stopped after scanning {plural(total, 'track')}."
        return

    wrote = (res.get("wrote-synced", 0) + res.get("wrote-plain", 0)
             + res.get("dry:wrote-synced", 0) + res.get("dry:wrote-plain", 0))
    not_found = res.get("not-found", 0)
    unavailable = res.get("providers-unavailable", 0)
    parts = [f"{plural(total, 'track')} scanned", f"{wrote} got lyrics"]
    if not_found:
        parts.append(f"{not_found} not found")
    if unavailable:
        parts.append(f"{unavailable} couldn't reach a provider (re-run later)")
    job.summary = " · ".join(parts) + "."
    log.info(job.summary)


# ── Library migration ──────────────────────────────────────────────────────────

def scan_migration(job, src, dest, *, use_acoustid, in_place=False):
    """Analyze the source library and attach one candidate per placeable album.

    Placeable albums become the review list (grouped by artist); files that
    can't be identified or that would collide are reported in the summary and
    left untouched. A preview manifest is written to the destination so the plan
    is auditable before anything is copied.
    """
    from qobuz_librarian.library import migrate as engine

    src, dest = Path(src), Path(dest)
    items = engine.collect_items(
        src, use_acoustid=use_acoustid,
        cancel_check=lambda: job.cancel_requested,
        progress=job.push_progress)
    if job.cancel_requested:
        n = len(items) if items else 0
        job.summary = (f"Stopped early. {plural(n, 'file')} scanned so far."
                       if n else "Stopped before anything was scanned.")
        return
    plan = engine.build_plan(items, dest)

    manifest = dest / "migration-manifest.csv"
    try:
        engine.write_manifest(plan, manifest)
    except OSError as exc:
        log.info(f"Couldn't write the preview manifest: {exc}")

    groups: dict = {}
    for entry in plan.placed:
        # dest_rel is <artist>/<album (year)>/[Disc N/]<track>; group by album dir.
        key = (entry.dest_rel.parts[0], entry.dest_rel.parts[1])
        groups.setdefault(key, []).append(entry)
    for (artist, album), entries in sorted(groups.items()):
        job.add_candidate(
            kind="migrate",
            title=album,
            artist=artist,
            detail=f"{plural(len(entries), 'track')} → {artist}/{album}",
            payload={"entries": [(str(e.source), str(e.dest_rel)) for e in entries]},
        )

    s = plan.summary()
    verb = "move" if in_place else "copy"
    parts = [f"{plural(s['place'], 'file')} ready to {verb}"]
    if s["unplaceable"]:
        parts.append(f"{s['unplaceable']} couldn't be identified")
    if s["collision"]:
        parts.append(f"{s['collision']} skipped to avoid name collisions")
    need, free = engine.space_estimate(plan, in_place=in_place)
    if need and free is not None:
        space = f"≈{format_size(need)} to {verb}, {format_size(free)} free at the destination"
        if need > free:
            space = ("⚠ not enough free space: needs "
                     f"≈{format_size(need)} but only {format_size(free)} is free")
            if in_place:
                space += (". The in-place move is blocked unless you re-run with "
                          "“proceed even if low on space” checked")
        parts.append(space)
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   f"where they are. Full plan written to {manifest}.")
    log.info(job.summary)


def execute_migration(job, chosen, dest, *, in_place, src=None,
                      allow_low_space=False):
    """Copy (or move) the files behind the approved albums into the layout."""
    from qobuz_librarian.library import migrate as engine

    dest = Path(dest)
    entries = []
    for c in chosen:
        for src_s, dest_s in c.get("payload", {}).get("entries", []):
            entries.append(engine.PlanEntry(
                source=Path(src_s), status=engine.PLACE, dest_rel=Path(dest_s)))
    if not entries:
        job.push_line("Nothing selected. Nothing to copy.")
        return

    plan = engine.MigrationPlan(dest_root=dest, entries=entries)
    # Re-check free space against the actually-selected files right before
    # touching anything (the user may have deselected albums since the scan, and
    # the disk may have changed). An in-place move that runs out mid-run leaves
    # the library half-relocated, so block a known-short in-place migration here
    # unless the user deliberately overrode the warning — the same gate the CLI
    # enforces with a typed "yes". Copy mode leaves the originals intact, so a
    # partial copy is recoverable and only warns.
    need, free = engine.space_estimate(plan, in_place=in_place)
    if in_place and free is not None and need > free and not allow_low_space:
        job.error = (
            f"Not enough free space at {dest}: the move needs about "
            f"{format_size(need)} but only {format_size(free)} is free. An "
            "in-place move that runs out mid-run would leave your library "
            "half-relocated. Free up space, choose another destination, or "
            "re-run the migration with “proceed even if low on space” checked.")
        job.summary = job.error
        log.info(job.summary)
        return
    # Serialize the file moves under the staging lock like every other execute
    # flow. Without it a migration writing into the library tree could interleave
    # with a concurrent download lane importing into the same <artist>/<album>
    # path — the exact race the "staging_lock serializes everything that touches
    # the tree" model is meant to prevent.
    from qobuz_librarian.web.jobs import staging_lock
    with staging_lock():
        result = engine.execute_plan(
            plan, in_place=in_place,
            cancel_check=lambda: job.cancel_requested,
            progress=job.push_progress)
        # In-place leaves the emptied source folders behind; clear the husk.
        pruned = engine.prune_empty_dirs(src) if (in_place and src) else 0
    # Leave the preview manifest (the full plan, including what was left behind)
    # alone; record what this run actually did in a sibling results file.
    try:
        engine.write_results_manifest(result, dest / "migration-results.csv")
    except OSError:
        pass
    for failed_src, reason in result.failures[:50]:
        job.push_line(f"failed: {failed_src} — {reason}")

    verb = "moved" if in_place else "copied"
    parts = [f"{plural(result.copied, 'file')} {verb} into {dest}"]
    if result.skipped:
        parts.append(f"{result.skipped} skipped (already present)")
    if result.lingered:
        parts.append(f"{result.lingered} moved but the original couldn't be removed")
    if result.failed:
        parts.append(f"{result.failed} failed; see the log")
        # Set job.error too (not just the prose summary) so a migration with
        # failed copies ends red, like every other execute path, instead of a
        # green DONE that buries "N failed" mid-sentence.
        job.error = f"{plural(result.failed, 'file')} couldn't be migrated; see the log."
    if pruned:
        parts.append(f"cleared {plural(pruned, 'empty source folder')}")
    if result.cancelled:
        parts.append("stopped early")
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
