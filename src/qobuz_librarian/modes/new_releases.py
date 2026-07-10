"""CLI new-release check — surface what's been added to Qobuz since the last
check across all library artists.

Uses the same engine as the web dashboard's auto-check (the cheap one-call-per-
artist diff against the last-seen catalog ids), just printed to the terminal
instead of routed through a job's candidate list. Cancellable with Ctrl-C.
``--dry-run`` skips advancing the baseline so a user can preview without losing
the chance to see the same releases again on the next run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    NoCredsError,
    QobuzUnavailable,
    load_qobuz_token,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_new_releases_for_artist,
    flush_resolve_cache,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_library_artists,
)
from qobuz_librarian.ui_cli.colors import C, fmt, section
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, die, plural
from qobuz_librarian.ui_cli.logging import log


def run_check_new_releases_mode(args):
    """Walk library artists and report what's new on Qobuz since the last check.

    Mirrors the engine the web auto-check uses, so a run advances the same
    'seen' baseline and the dashboard's age line picks it up. First run on a
    library records the baseline and surfaces nothing — same first-touch
    contract as the web.
    """
    try:
        _user_id, token = load_qobuz_token()
    except NoCredsError:
        die(fmt(C.RED,
            "✗  No Qobuz credentials configured.\n"
            f"   Paste your user_auth_token on the Settings page "
            f"(http://<host>:{cfg.WEB_PORT}/settings)\n"
            "   or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"),
            EXIT_AUTH)

    if not new_releases_mod.is_baseline_complete():
        die(fmt(C.YELLOW,
            "No new-release baseline exists yet. Run a Library refresh first."),
            EXIT_GENERAL)

    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info(fmt(C.YELLOW, "No artist folders found under MUSIC_ROOT."))
        return

    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    hidden = hidden_mod.load()
    # Single-track marks only suppress artists when the setting is on — the
    # same gate the web check applies, so the two report the same artists.
    single_store = hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
    # A grown catalog cap means the old baseline is missing everything past
    # the previous limit; re-baseline instead of dumping that slice as "new"
    # (mirrors the web check's guard).
    cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
    prev_limit = state.get("baseline_limit")
    rebaseline = prev_limit is None or cur_limit > int(prev_limit)
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)

    section(f"New-release check — {plural(len(artists), 'artist')}")
    if not seen:
        log.info(fmt(C.GRAY,
            "  First check on this library — recording the current catalog "
            "as the baseline. Nothing surfaces this run; later runs diff "
            "against this snapshot."))

    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    current_seen = {}
    total_new = 0
    artists_with_news = 0
    had_failure = False

    # Managed explicitly rather than via `with ... as ex` so a Ctrl-C / AuthLost
    # doesn't get stuck in the context manager's implicit shutdown(wait=True),
    # which blocks until every in-flight network call returns (seconds-to-minutes
    # against a slow/rate-limited Qobuz). The finally instead discards queued
    # tasks and lets the few running requests finish detached.
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel")
    try:
        futures = {ex.submit(find_new_releases_for_artist, ad.name,
                             token=token, opts=opts, seen_by_id=seen,
                             hidden=hidden, single_store=single_store,
                             artist_dir=ad, baseline_only=rebaseline): ad
                   for ad in artists}
        for fut in as_completed(futures):
            ad = futures[fut]
            try:
                result = fut.result()
            except (AuthLost, QobuzUnavailable):
                raise
            except Exception as e:
                log.info(fmt(C.GRAY, f"    skipped {ad.name}: {e}"))
                had_failure = True
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
            else:
                had_failure = True
            if result.new_gaps:
                artists_with_news += 1
                log.info(fmt(C.GREEN,
                    f"  {result.artist_name} — "
                    f"{plural(len(result.new_gaps), 'new release')}"))
                for gap in result.new_gaps:
                    a = gap.qobuz_album
                    title = a.get("title") or "?"
                    year = (str(a.get("release_date_original") or "")[:4]
                            or "?")
                    log.info(fmt(C.WHITE, f"    • {title} ({year})"))
                total_new += len(result.new_gaps)
    except KeyboardInterrupt:
        log.info(fmt(C.YELLOW, "\n  ⚠  Cancelled — not recording this run."))
        raise
    finally:
        # wait=False: never block on in-flight calls. cancel_futures discards
        # any not-yet-started task. On the success path every future is already
        # done, so this is just a clean teardown.
        ex.shutdown(wait=False, cancel_futures=True)

    flush_resolve_cache()
    log.info("")
    if rebaseline and seen:
        log.info(fmt(C.GRAY,
            "  · Catalogue limit changed — recorded a fresh baseline; "
            "future checks diff against it."))
    elif total_new:
        log.info(fmt(C.BOLD + C.WHITE,
            f"  ✓  {plural(total_new, 'new release')} across "
            f"{plural(artists_with_news, 'artist')}."))
        log.info(fmt(C.GRAY,
            "  Run the web library scan or the per-artist scan to queue them."))
    elif seen:
        log.info(fmt(C.GRAY, "  · Nothing new found."))
    else:
        log.info(fmt(C.GRAY, "  · Baseline recorded."))

    if not args.dry_run:
        # UNION each reached artist's snapshot into the prior baseline rather
        # than replacing it — an over-cap catalog comes back as a different
        # slice each run, so a replace would let rotated-out ids re-surface as
        # "new" (the same merge the web flow does). An artist that errored
        # keeps its old entry. Only claim a COMPLETE baseline when the crawl
        # actually reached artists cleanly: marking complete after an
        # all-skipped/errored run would seed an empty baseline and permanently
        # defeat the library scan's seeding.
        merged = dict(seen)
        for aid, ids in current_seen.items():
            merged[aid] = sorted(set(merged.get(aid, [])) | set(ids))
        complete = not had_failure and bool(current_seen)
        new_releases_mod.mark_run(merged, complete=complete,
                                  baseline_limit=cur_limit)
