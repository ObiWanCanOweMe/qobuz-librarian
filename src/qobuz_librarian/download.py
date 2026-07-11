"""Download phase for one album: pick a strategy, rip, drop lossy/broken
files, retry the strays once, and reconcile the counts.

Shared by the single-album path (`modes/process.py`) and the queue executor
(`queue/executor.py`). Both hand it a staging snapshot and the missing/present
split and read back the `n_ok`/`n_fail`/`n_lossy` bookkeeping. Results are
written into a caller-owned `result` dict as the work progresses — not just
returned — so the gap-fill backup taken mid-download can still be resolved by
the caller's finally/except when a rip raises AuthLost or hits a full disk.
"""
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    detect_auth_lost,
    detect_disk_full,
    detect_rate_limited,
)
from qobuz_librarian.integrations.rip import (
    cleanup_lossy,
    files_added_since,
    is_cancel_requested,
    rip_url,
    snapshot_staging,
)
from qobuz_librarian.library.backup import backup_gap_fill_files
from qobuz_librarian.library.catalog import find_extras_in_existing
from qobuz_librarian.library.scanner import read_album_dir, read_audio_meta
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog


def match_key_from_stem(p):
    """Normalized title key from a filename stem (or bare stem string) used to
    line a downloaded/deleted file up against its Qobuz track.

    Accepts a Path or a bare stem string. A Path's ``.stem`` would mis-split a
    title like "01. ★" (pathlib reads ". ★" as a suffix), so an already-extracted
    string is taken verbatim. Strips a leading
    "<disc>-<track>"/"<track>" number and any "Artist - " prefix streamrip
    writes, then runs the result through the same normalize/strip_edition_suffix
    a Qobuz title goes through, so the two sides compare on equal terms."""
    s = p.stem if hasattr(p, "stem") else str(p)
    m = re.match(r"^(?:\d+[-.])?\d+[\s\-–—.]+(.+)$", s)
    t = m.group(1) if m else s
    m = re.match(r"^.+?\s+-\s+(.+)$", t)
    return normalize(strip_edition_suffix(m.group(1) if m else t))


def _bare_title(title):
    return normalize(strip_edition_suffix(title or ""))


_DISC_DIR_RE = re.compile(r"^(?:disc|cd)\s*0*(\d+)\b", re.IGNORECASE)
_NUMBERED_STEM_RE = re.compile(
    r"^(?:(\d+)[-.])?(\d+)(?:\s*[-–—.]\s*|\s+)(.+)$")


def _positive_int(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_isrc(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _file_track_identity(path, context_tracks):
    """Best stable identity available before a rejected file is deleted."""
    path = Path(path)
    stem_match = _NUMBERED_STEM_RE.match(path.stem)
    stem_disc = _positive_int(stem_match.group(1)) if stem_match else None
    stem_track = _positive_int(stem_match.group(2)) if stem_match else None
    parent_match = _DISC_DIR_RE.match(path.parent.name)
    parent_disc = _positive_int(parent_match.group(1)) if parent_match else None

    try:
        meta = read_audio_meta(path) if path.exists() else None
    except OSError as exc:
        vlog(f"retry identity: couldn't read {path}: {exc}")
        meta = None
    meta = meta or {}

    meta_track = _positive_int(meta.get("tracknumber"))
    track_conflict = bool(
        meta_track and stem_track and meta_track != stem_track)
    if track_conflict:
        track = None
    else:
        track = meta_track or stem_track

    context_discs = {
        _positive_int(track.get("media_number")) or 1
        for track in context_tracks
    }
    meta_disc = _positive_int(meta.get("discnumber"))
    # read_audio_meta defaults a missing DISCNUMBER to 1. On a multi-disc album
    # that is not evidence of Disc 1, so use it only when it is non-default or
    # the Qobuz album itself has one disc.
    if meta_disc == 1 and len(context_discs) > 1:
        meta_disc = None
    explicit_disc = parent_disc or stem_disc
    if parent_disc and stem_disc and parent_disc != stem_disc:
        explicit_disc = None
        disc_conflict = True
    else:
        disc_conflict = False
    if explicit_disc and meta_disc and explicit_disc != meta_disc:
        disc = None
        disc_conflict = True
    else:
        disc = explicit_disc or meta_disc
    if disc is None and not disc_conflict and len(context_discs) == 1:
        disc = next(iter(context_discs))

    title = _bare_title(meta.get("title"))
    if not title:
        title = match_key_from_stem(path)
    return {
        "isrc": _clean_isrc(meta.get("isrc")),
        "position": (disc, track) if disc and track else None,
        "track": track,
        "title": title,
        "conflicted": track_conflict or disc_conflict,
    }


def _track_identity(track):
    disc = _positive_int(track.get("media_number")) or 1
    number = _positive_int(track.get("track_number"))
    return {
        "isrc": _clean_isrc(track.get("isrc")),
        "position": (disc, number) if number else None,
        "track": number,
        "title": _bare_title(track.get("title")),
    }


def _capture_file_identities(paths, context_tracks):
    return {str(Path(path)): _file_track_identity(path, context_tracks)
            for path in paths}


def _pair_files_to_tracks(paths, tracks, identities, context_tracks):
    """Pair downloaded files to Qobuz tracks without guessing at twins.

    Strong identity locks a file: an explicit disc/track that names another
    album slot cannot fall through to a convenient title match. Weaker track
    number and title matching is allowed only when that key is unique across
    the complete Qobuz context.
    """
    paths = list(paths)
    tracks = list(tracks)
    if not paths or not tracks:
        return []
    context_ids = [_track_identity(track) for track in context_tracks]
    context_counts = {
        layer: Counter(identity.get(layer) for identity in context_ids
                       if identity.get(layer) is not None)
        for layer in ("isrc", "position", "track", "title")
    }
    file_ids = [identities.get(str(Path(path)))
                or _file_track_identity(path, context_tracks) for path in paths]
    track_ids = [_track_identity(track) for track in tracks]

    def allowed_layer(identity):
        if identity.get("conflicted"):
            return None
        isrc = identity.get("isrc")
        if isrc and context_counts["isrc"].get(isrc, 0) == 1:
            return "isrc"
        if identity.get("position") is not None:
            return "position"
        number = identity.get("track")
        if number is not None and context_counts["track"].get(number, 0) == 1:
            return "track"
        title = identity.get("title")
        if title and context_counts["title"].get(title, 0) == 1:
            return "title"
        return None

    remaining_files = set(range(len(paths)))
    remaining_tracks = set(range(len(tracks)))
    pairs = []
    for layer in ("isrc", "position", "track", "title"):
        file_groups = {}
        track_groups = {}
        for index in remaining_files:
            identity = file_ids[index]
            key = identity.get(layer)
            if key is not None and allowed_layer(identity) == layer:
                file_groups.setdefault(key, []).append(index)
        for index in remaining_tracks:
            key = track_ids[index].get(layer)
            if key is not None:
                track_groups.setdefault(key, []).append(index)
        for key, file_group in file_groups.items():
            track_group = track_groups.get(key, [])
            if (context_counts[layer].get(key, 0) != 1
                    or len(file_group) != 1 or len(track_group) != 1):
                continue
            file_index, track_index = file_group[0], track_group[0]
            if file_index not in remaining_files or track_index not in remaining_tracks:
                continue
            remaining_files.remove(file_index)
            remaining_tracks.remove(track_index)
            pairs.append((paths[file_index], tracks[track_index]))
    return pairs


def _remove_reject(bucket, rejected):
    for index, item in enumerate(bucket):
        if item == rejected:
            del bucket[index]
            return True
    return False


def _reject_label(path):
    return path.stem if isinstance(path, Path) else str(path)


def run_album_download(*, album, missing, present, album_dir, snapshot,
                       existing=None, quality=None, upgrade_only=False,
                       force_track_by_track=False, result=None):
    """Download ``missing`` for one album and reconcile what actually landed.

    Picks a single full-album rip when most of the album is missing, else
    fetches track by track. ``existing`` is the on-disk track list (dicts with
    "path") used to stash already-owned tracks before a full-album re-rip;
    pass None to have it read from ``album_dir`` only if that branch is reached.

    Writes into ``result`` (created if None) as it goes — ``gap_fill_backup_path``
    the moment the backup is taken, then n_ok / n_fail / n_lossy /
    failed_tracks / lossy_tracks / rate_limited / elapsed / download_full_album /
    full_album_rc at the end — and returns it. Honours is_cancel_requested() to
    stop early; raises AuthLost on auth loss and OSError(ENOSPC) on a full disk
    for the caller to handle."""
    if result is None:
        result = {}
    result.setdefault("gap_fill_backup_path", None)

    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    n_tracks_total = len(qobuz_tracks)

    # Streamrip's track-URL path crashes with KeyError: 'body' on some tracks
    # (older catalog, edge metadata), so prefer the album URL when most of the
    # album is missing — beets merges any redundant duplicate of a present
    # track on import. Small gap-fills stay track-by-track. Repair pins
    # per-track no matter the ratio, so a tweak here can't turn a targeted
    # truncation-repair into a wipe-and-replace.
    if force_track_by_track:
        download_full_album = False
    elif upgrade_only:
        download_full_album = (len(missing) == n_tracks_total)
    else:
        download_full_album = (
            len(present) == 0
            or len(missing) >= max(4, int(n_tracks_total * 0.7))
        )

    album_id = album.get("id")
    t_start = time.time()
    n_fail = 0
    failed_tracks = []
    # Keep failed tracks as their Qobuz objects. Titles are display text, not
    # identity: two requested tracks can share one across discs or versions.
    failed_track_objs = []
    full_album_rc = None
    rate_limited = False

    if download_full_album:
        log.info(fmt(C.GRAY,
            f"  Strategy: full-album URL "
            f"({len(missing)} of {n_tracks_total} missing)"))
    else:
        why = ("forced per-track (repair)" if force_track_by_track
               else f"{len(missing)} of {n_tracks_total} missing")
        log.info(fmt(C.GRAY, f"  Strategy: per-track ({why})"))

    # Free-space preflight. streamrip reports a full disk only via stderr text,
    # which detect_disk_full() best-effort-greps — a real ENOSPC whose message
    # lacks the errno string would otherwise read as an ordinary per-track
    # failure and march the whole queue into the same wall, deleting each
    # truncated partial as it goes. Abort early down the proper disk-full path
    # (errno 28 → the caller restores backups and keeps items for a retry once
    # space is freed) when staging is below the floor.
    if cfg.MIN_FREE_STAGING_MB > 0:
        try:
            free_mb = shutil.disk_usage(cfg.STAGING_DIR).free // (1024 * 1024)
        except OSError:
            free_mb = None
        if free_mb is not None and free_mb < cfg.MIN_FREE_STAGING_MB:
            raise OSError(
                28,
                f"Only {free_mb} MB free at {cfg.STAGING_DIR} "
                f"(below the {cfg.MIN_FREE_STAGING_MB} MB MIN_FREE_STAGING_MB "
                f"floor) — refusing to start the download.")

    if download_full_album:
        url = f"https://play.qobuz.com/album/{album_id}"
        section("Downloading full album")
        report_progress("Downloading album", 0, 0,
                        f"{album.get('title') or '?'} · {n_tracks_total} tracks")
        vlog(f"  ⟳  {url}")
        # Move the already-present tracks to a backup before the rip so beets
        # doesn't create 'Foo.1.flac' duplicates on import, and so a rip
        # failure (network drop, Ctrl+C, auth loss) can't leave the user with
        # permanently lost tracks. The caller restores it if we don't fully
        # succeed; recording it now keeps that recovery reachable on a raise.
        if present and album_dir:
            ex = existing if existing is not None else read_album_dir(album_dir)
            extra_paths = {e["path"]
                           for e in find_extras_in_existing(qobuz_tracks, ex)}
            to_clear = [e for e in ex if e["path"] not in extra_paths]
            if to_clear:
                vlog(f"pre-download: backing up + removing {len(to_clear)} present "
                     f"track(s) to prevent .1.flac collisions")
                result["gap_fill_backup_path"] = backup_gap_fill_files(
                    [e["path"] for e in to_clear], album_dir)
        rc, out = rip_url(url, timeout=cfg.RIP_TIMEOUT, live_output=True,
                          quality=quality)
        full_album_rc = rc
        if detect_auth_lost(out):
            raise AuthLost("rip output contained auth-lost markers")
        if detect_disk_full(out):
            raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
        rate_limited = rate_limited or detect_rate_limited(out)
        # rip exits 0 even when it skipped tracks after persistent retries;
        # count the ERROR markers so a "succeeded" line can't hide a gap.
        n_errors = len(re.findall(
            r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?ERROR\b", out, re.MULTILINE))
        if rc != 0:
            log.info(fmt(C.RED, f"  ✗  rip exit {rc}; last 300 chars:"))
            log.info(fmt(C.GRAY, "  " + out[-300:].replace("\n", "\n  ")))
        elif n_errors:
            log.info(fmt(C.YELLOW,
                f"  ⚠  rip exit 0 but {n_errors} error(s) in output — "
                f"some tracks likely skipped (see summary below)."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Download succeeded."))
    else:
        section("Downloading missing tracks")
        for i, t in enumerate(missing, 1):
            if is_cancel_requested():
                break
            tid = t.get("id")
            # Show the version + track number so an EP of same-titled remixes
            # doesn't render as N identical lines that look like a dup-download.
            ttl = t.get("title") or "?"
            ver = t.get("version") or ""
            if ver and ver.lower() not in ttl.lower():
                ttl = f"{ttl} ({ver})"
            tnum = t.get("track_number")
            tnum_prefix = f"#{tnum:>2} · " if tnum else ""
            log.info(fmt(C.BLUE, f"\n  [{i}/{len(missing)}]") +
                     f"  {fmt(C.WHITE, truncate(tnum_prefix + ttl, 60))}")
            report_progress("Downloading", i, len(missing), ttl)
            rc, out = rip_url(f"https://play.qobuz.com/track/{tid}",
                              timeout=cfg.RIP_TIMEOUT, quality=quality)
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            if rc == 0:
                log.info(fmt(C.GREEN, "    ✓ ok"))
            elif is_cancel_requested():
                # rip exited because we asked it to stop, not a real failure.
                break
            else:
                failed_track_objs.append(t)
                if "KeyError: 'body'" in out:
                    log.info(fmt(C.RED,
                        "    ✗ streamrip KeyError on track endpoint "
                        "(known bug; usually works via album URL)."))
                else:
                    log.info(fmt(C.RED, f"    ✗ rip exit {rc}"))
                    log.info(fmt(C.GRAY, "      " + out[-200:].replace("\n", " ")))
            # Qobuz throttles sustained per-track pulls; when the last rip shows
            # throttle signals, pause longer before the next so we stop pounding
            # the limit (set RATE_LIMIT_COOLDOWN=0 to disable).
            cooldown = cfg.RATE_LIMIT_COOLDOWN if detect_rate_limited(out) else 0
            if cooldown and i < len(missing):
                log.info(fmt(C.YELLOW,
                    f"    ⏳ Qobuz rate-limit detected — cooling down "
                    f"{int(cooldown)}s before the next track."))
                time.sleep(cooldown)
            else:
                time.sleep(cfg.DELAY_BETWEEN)

    new_files = files_added_since(snapshot)
    audio_new = [f for f in new_files if f.suffix.lower() in cfg.AUDIO_EXTS]
    vlog(f"  {len(new_files)} new file(s) in staging ({len(audio_new)} audio)")
    # cleanup_lossy removes rejects, so capture their tags and path-derived
    # disc/track identity first. A title alone is not safe for same-title twins.
    file_identities = _capture_file_identities(audio_new, qobuz_tracks)
    kept, lossy, broken = cleanup_lossy(audio_new)
    n_ok = len(kept)
    attempted_tracks = qobuz_tracks if download_full_album else missing
    retried_clean_targets = set()

    # Both reject kinds get one per-track retry: a broken FLAC is usually a
    # transient glitch, and the album URL occasionally serves lossy for a track
    # the track URL has lossless. One retry per track — no recursion, no loop.
    # Skipped once a cancel is in flight so we don't fire rips the user stopped.
    discarded = lossy + broken
    if discarded and attempted_tracks and not is_cancel_requested():
        retry_pairs = _pair_files_to_tracks(
            discarded, attempted_tracks, file_identities, qobuz_tracks)
        if retry_pairs:
            log.info(fmt(C.GRAY,
                f"  ↻  Retrying {len(retry_pairs)} lossy/incomplete "
                "track(s) once via per-track URL"))
            recovered = 0
            for rejected, t in retry_pairs:
                if is_cancel_requested():
                    break
                tid = t.get("id")
                if not tid:
                    continue
                # Collect this target independently. A clean same-title file
                # produced by another retry must never vouch for this one.
                retry_snapshot = snapshot_staging()
                rc, out = rip_url(f"https://play.qobuz.com/track/{tid}",
                                  timeout=cfg.RIP_TIMEOUT, quality=quality)
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                retry_audio = [f for f in files_added_since(retry_snapshot)
                               if f.suffix.lower() in cfg.AUDIO_EXTS]
                retry_identities = _capture_file_identities(
                    retry_audio, qobuz_tracks)
                retry_kept, _, _ = cleanup_lossy(retry_audio)
                matches = _pair_files_to_tracks(
                    retry_kept, [t], retry_identities, qobuz_tracks)
                if not matches:
                    continue
                recovered_path, _ = matches[0]
                removed = (_remove_reject(lossy, rejected)
                           or _remove_reject(broken, rejected))
                if not removed:
                    continue
                kept.append(recovered_path)
                file_identities.update(retry_identities)
                retried_clean_targets.add(id(t))
                recovered += 1
            if recovered:
                n_ok = len(kept)
                log.info(fmt(C.GREEN,
                    f"  ✓  Retry recovered {recovered} track(s)"))

    # A HARD failure (rip errored with no file landing at all — distinct from a
    # file that landed lossy/broken, retried above) gets one more per-track pull
    # before it's given up on: a transient 5xx / momentary network blip usually
    # clears on a second attempt, and otherwise the user has to re-run the whole
    # repair or download just for that one track. One retry, no loop; the
    # reconcile below un-fails anything this lands. Skipped on a cancel, and only
    # for tracks that still have no clean file on disk.
    if failed_track_objs and missing and not is_cancel_requested():
        clean_failed_ids = {
            id(track) for _, track in _pair_files_to_tracks(
                kept, failed_track_objs, file_identities, qobuz_tracks)
        }
        rejected_failed_ids = {
            id(track) for _, track in _pair_files_to_tracks(
                lossy + broken, failed_track_objs, file_identities, qobuz_tracks)
        }
        hard_targets = [
            track for track in failed_track_objs
            if id(track) not in clean_failed_ids
            and id(track) not in rejected_failed_ids
            and track.get("id")
        ]
        if hard_targets:
            log.info(fmt(C.GRAY,
                f"  ↻  Retrying {len(hard_targets)} failed download(s) once "
                "via per-track URL"))
            recovered = 0
            for t in hard_targets:
                if is_cancel_requested():
                    break
                hard_snapshot = snapshot_staging()
                rc, out = rip_url(f"https://play.qobuz.com/track/{t['id']}",
                                  timeout=cfg.RIP_TIMEOUT, quality=quality)
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                time.sleep(cfg.DELAY_BETWEEN)
                hard_audio = [f for f in files_added_since(hard_snapshot)
                              if f.suffix.lower() in cfg.AUDIO_EXTS]
                hard_identities = _capture_file_identities(
                    hard_audio, qobuz_tracks)
                hard_kept, hard_lossy, hard_broken = cleanup_lossy(hard_audio)
                matches = _pair_files_to_tracks(
                    hard_kept, [t], hard_identities, qobuz_tracks)
                if matches:
                    kept.append(matches[0][0])
                    file_identities.update(hard_identities)
                    retried_clean_targets.add(id(t))
                    recovered += 1
                    continue
                # Preserve an exact reject from the hard retry in the right
                # summary bucket. Unexpected or ambiguous files prove nothing.
                reject_matches = _pair_files_to_tracks(
                    hard_lossy + hard_broken, [t], hard_identities,
                    qobuz_tracks)
                if reject_matches:
                    rejected_path = reject_matches[0][0]
                    (lossy if rejected_path in hard_lossy else broken).append(
                        rejected_path)
                    file_identities.update(hard_identities)
            if recovered:
                n_ok = len(kept)
                log.info(fmt(C.GREEN,
                    f"  ✓  Retry recovered {recovered} failed download(s)"))

    # Both reject kinds count against album completeness. Keep them as Paths
    # through reconciliation; broken tracks remain a distinct display subset.
    lossy_tracks = lossy + broken
    n_lossy = len(lossy_tracks)

    # Reconcile per-track failures with exact files. A rip can exit non-zero
    # after landing a valid FLAC, but a same-title sibling is not evidence.
    if not download_full_album and failed_track_objs:
        clean_failed_ids = {
            id(track) for _, track in _pair_files_to_tracks(
                kept, failed_track_objs, file_identities, qobuz_tracks)
        }
        rejected_failed_ids = {
            id(track) for _, track in _pair_files_to_tracks(
                lossy_tracks, failed_track_objs, file_identities, qobuz_tracks)
        }
        still_failed = [
            track for track in failed_track_objs
            if id(track) not in clean_failed_ids
            and id(track) not in rejected_failed_ids
        ]
        landed_despite_error = clean_failed_ids - retried_clean_targets
        if landed_despite_error:
            log.info(fmt(C.GRAY,
                f"  · {len(landed_despite_error)} track(s) landed despite a streamrip "
                f"post-processing error — counting as success."))
        failed_tracks = [track.get("title") or "?" for track in still_failed]
        n_fail = len(still_failed)

    if download_full_album and full_album_rc is not None:
        # A full-album rip re-downloads the WHOLE album URL (all n_tracks_total
        # tracks), including the already-present ones we moved to the gap-fill
        # backup — so n_ok (every clean FLAC that landed) is counted against the
        # total, NOT len(missing). Using len(missing) here let a present track's
        # re-rip failure clamp n_fail to 0, which would (a) read an incomplete
        # fill as clean and (b) let the executor drop the gap-fill backup or a
        # sibling that still holds the missing track. A lossy fallback counts
        # once in the lossy bucket, so n_ok + n_lossy + n_fail == tracks attempted.
        n_fail = max(0, n_tracks_total - n_ok - n_lossy)
        if n_fail > 0:
            accounted = {
                id(track) for _, track in _pair_files_to_tracks(
                    kept + lossy_tracks, qobuz_tracks, file_identities,
                    qobuz_tracks)
            }
            failed_tracks = [
                track.get("title") or "?" for track in qobuz_tracks
                if id(track) not in accounted
            ][:n_fail]
        else:
            failed_tracks = []
            if full_album_rc != 0 and n_ok > 0:
                log.info(fmt(C.GRAY,
                    f"  · {n_ok} track(s) landed despite rip exit "
                    f"{full_album_rc} (streamrip post-processing error)."))

    if lossy:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(lossy)} track(s) only available lossy on Qobuz "
            f"(no lossless for your tier — another source needed):"))
        for d in lossy[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))
    if broken:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(broken)} track(s) downloaded incomplete and were "
            f"discarded (a re-run usually fixes these):"))
        for d in broken[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))

    # Paths are retained only inside this download phase. The queue, activity
    # log, and CLI have always exposed short display strings.
    lossy_track_labels = [_reject_label(path) for path in lossy_tracks]
    broken_track_labels = [_reject_label(path) for path in broken]

    result.update({
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": n_lossy,
        "failed_tracks": failed_tracks,
        "lossy_tracks": lossy_track_labels,
        "broken_tracks": broken_track_labels,
        "rate_limited": rate_limited,
        "elapsed": time.time() - t_start,
        "download_full_album": download_full_album,
        "full_album_rc": full_album_rc,
    })
    return result
