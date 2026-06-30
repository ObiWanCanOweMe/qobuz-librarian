"""Staged-rip quality checks before local rewrite hooks run."""
import shutil
import tempfile
from pathlib import Path

from qobuz_librarian.library.catalog import read_album_dir
from qobuz_librarian.quality.decision import album_max_quality


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


def redownload_with_staged_fallback(staged_dirs, *, discard_retry_output,
                                    run_retry, collect_staged_dirs):
    """Retry without losing the first usable staged rip if retry lands nothing.

    Returns ``(staged_dirs, retry_kept)``.
    """
    originals = [Path(d) for d in staged_dirs if Path(d).exists()]
    if not originals:
        run_retry()
        return collect_staged_dirs(), True

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
        for idx, original in enumerate(originals):
            parked = Path(tmp) / str(idx)
            shutil.move(str(original), str(parked))
            moved.append((original, parked))
        try:
            run_retry()
            fresh = collect_staged_dirs()
        except BaseException:
            restore(moved)
            raise
        if fresh:
            return fresh, True
        return restore(moved), False
