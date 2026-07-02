"""Staged-rip quality checks before local rewrite hooks run."""
import shutil
import tempfile
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.catalog import read_album_dir
from qobuz_librarian.quality.decision import album_max_quality
from qobuz_librarian.ui_cli.logging import log


def staged_track_qualities(staged_dirs):
    quals, n_unknown = [], 0
    for d in staged_dirs:
        for t in read_album_dir(d):
            bits = t.get("bits") or 0
            rate = t.get("sample_rate") or 0
            if bits and rate:
                quals.append((int(bits), int(rate)))
            else:
                n_unknown += 1
    return quals, n_unknown


def rip_shortfall(staged_dirs, qobuz_album, *, effective_tier=None):
    """Decide whether the staged rip came in below min(source, cap)."""
    target = album_max_quality(qobuz_album, tier=effective_tier)
    quals, n_unknown = staged_track_qualities(staged_dirs)
    n_below = sum(1 for q in quals if q < target)
    return {
        "under": n_below > 0,
        "n_below": n_below,
        "target": target,
        "worst": min(quals) if quals else None,
        "n_unknown": n_unknown,
    }


def verify_and_recover(qobuz_album, staged_dirs, *, redownload_at_max,
                       effective_tier, allow_retry=True):
    """Verify the staged rip and retry once at the max tier when safe."""
    sf = rip_shortfall(staged_dirs, qobuz_album, effective_tier=effective_tier)
    recovered = retried = False
    if sf["under"] and effective_tier < 4 and allow_retry:
        retried = True
        fresh_dirs = redownload_at_max()
        if fresh_dirs:
            staged_dirs = fresh_dirs
            sf = rip_shortfall(staged_dirs, qobuz_album,
                               effective_tier=effective_tier)
            recovered = not sf["under"]
    return {
        "under": sf["under"],
        "recovered": recovered,
        "retried": retried,
        "n_below": sf["n_below"],
        "served": sf["worst"],
        "target": sf["target"],
        "staged_dirs": staged_dirs,
    }


def _staged_audio_count(dirs):
    """Audio files across staged dirs — the completeness yardstick for
    comparing the first rip against a retry."""
    n = 0
    for d in dirs:
        p = Path(d)
        try:
            if p.is_dir():
                n += sum(1 for f in p.rglob("*")
                         if f.is_file() and f.suffix.lower() in cfg.AUDIO_EXTS)
            elif p.is_file() and p.suffix.lower() in cfg.AUDIO_EXTS:
                n += 1
        except OSError:
            continue
    return n


def redownload_with_staged_fallback(staged_dirs, *, discard_retry_output,
                                    run_retry, collect_staged_dirs):
    """Retry without losing the first usable staged rip if retry lands nothing
    — or lands less of the album than the first rip did.

    Returns ``(staged_dirs, retry_kept)``.
    """
    originals = [Path(d) for d in staged_dirs if Path(d).exists()]
    if not originals:
        run_retry()
        return collect_staged_dirs(), True
    first_rip_tracks = _staged_audio_count(originals)

    def restore(moved):
        restored = []
        discard_retry_output()
        for original, parked in moved:
            if not parked.exists():
                continue
            if original.exists():
                if original.is_dir():
                    shutil.rmtree(original, ignore_errors=True)
                else:
                    try:
                        original.unlink()
                    except OSError:
                        pass
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(parked), str(original))
            restored.append(original)
        return restored

    with tempfile.TemporaryDirectory(prefix="qobuz-quality-retry-") as tmp:
        moved = []
        try:
            for idx, original in enumerate(originals):
                parked = Path(tmp) / str(idx)
                shutil.move(str(original), str(parked))
                moved.append((original, parked))
        except BaseException:
            # Parking itself failed (tmp full, permissions). Put back what was
            # already parked — NOT via restore(): no retry ran, and its
            # discard_retry_output diff would count the still-unparked
            # originals as retry output and delete the only rip.
            for original, parked in moved:
                if parked.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(parked), str(original))
            raise
        try:
            run_retry()
            fresh = collect_staged_dirs()
        except BaseException:
            restore(moved)
            raise
        if fresh:
            if _staged_audio_count(fresh) >= first_rip_tracks:
                return fresh, True
            # The max-tier retry landed fewer tracks than the first rip.
            # Trading a complete album for a partial higher-quality one loses
            # tracks the user already had — keep the first rip.
            log.info("  Higher-quality retry came back with fewer tracks than "
                     "the first rip; keeping the first rip.")
        return restore(moved), False
