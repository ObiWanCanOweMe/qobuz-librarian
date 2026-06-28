"""Small persistent state for navigation review markers."""

import json
import time

from qobuz_librarian import config as cfg

SURFACES = {"library", "upgrade", "downsample"}
_VERSION = 1


def _empty() -> dict:
    return {
        "version": _VERSION,
        "surfaces": {name: {"ready_at": 0.0, "seen_at": 0.0}
                     for name in sorted(SURFACES)},
    }


def load() -> dict:
    path = cfg.REVIEW_BADGE_STATE_FILE
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return _empty()
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, dict):
        return _empty()
    state = _empty()
    for name in SURFACES:
        entry = surfaces.get(name)
        if not isinstance(entry, dict):
            continue
        state["surfaces"][name] = {
            "ready_at": float(entry.get("ready_at") or 0.0),
            "seen_at": float(entry.get("seen_at") or 0.0),
        }
    return state


def _save(state: dict) -> None:
    path = cfg.REVIEW_BADGE_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def _ts(now=None) -> float:
    return float(time.time() if now is None else now)


def mark_ready(surface: str, *, now=None) -> None:
    if surface not in SURFACES:
        return
    state = load()
    entry = state["surfaces"][surface]
    seen_at = float(entry.get("seen_at") or 0.0)
    entry["ready_at"] = max(_ts(now), seen_at + 0.000001)
    _save(state)


def clear_ready(surface: str) -> None:
    if surface not in SURFACES:
        return
    state = load()
    state["surfaces"][surface]["ready_at"] = 0.0
    _save(state)


def set_ready(surface: str, ready: bool, *, now=None) -> None:
    if ready:
        mark_ready(surface, now=now)
    else:
        clear_ready(surface)


def mark_seen(surface: str, *, now=None) -> None:
    if surface not in SURFACES:
        return
    state = load()
    entry = state["surfaces"][surface]
    entry["seen_at"] = max(_ts(now), float(entry.get("ready_at") or 0.0))
    _save(state)


def snapshot() -> dict[str, bool]:
    state = load()
    out: dict[str, bool] = {}
    for name, entry in state["surfaces"].items():
        out[name] = (
            float(entry.get("ready_at") or 0.0)
            > float(entry.get("seen_at") or 0.0)
        )
    return out
