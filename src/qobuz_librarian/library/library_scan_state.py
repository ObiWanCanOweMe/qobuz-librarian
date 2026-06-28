"""Saved whole-library scan snapshot for cheap post-baseline refreshes."""
import hashlib
import json
import os
import tempfile
import time

from qobuz_librarian import config as cfg

STATE_VERSION = 1


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        "kinds": {},
    }


def _empty_kind():
    return {
        "updated_at": None,
        "complete": False,
        "hidden_signature": "",
        "artists": {},
    }


def hidden_signature(store, scope: str) -> str:
    bucket = (store or {}).get(scope) if isinstance(store, dict) else {}
    if not isinstance(bucket, dict):
        bucket = {}
    raw = json.dumps(bucket, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load():
    if not cfg.LIBRARY_SCAN_STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(cfg.LIBRARY_SCAN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    kinds = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
    base.update({
        "updated_at": data.get("updated_at"),
        "kinds": kinds,
    })
    return base


def kind_state(kind: str):
    data = load()
    bucket = (data.get("kinds") or {}).get(kind)
    if not isinstance(bucket, dict):
        return _empty_kind()
    base = _empty_kind()
    artists = bucket.get("artists") if isinstance(bucket.get("artists"), dict) else {}
    base.update({
        "updated_at": bucket.get("updated_at"),
        "complete": bool(bucket.get("complete")),
        "hidden_signature": str(bucket.get("hidden_signature") or ""),
        "artists": artists,
    })
    return base


def _write_state(data):
    try:
        cfg.LIBRARY_SCAN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(cfg.LIBRARY_SCAN_STATE_FILE.parent),
            prefix=".qobuz_library_scan_state.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, cfg.LIBRARY_SCAN_STATE_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass


def _clean_artist_state(entry):
    if not isinstance(entry, dict):
        entry = {}
    candidates = entry.get("candidates")
    catalog_ids = entry.get("catalog_ids")
    return {
        "fingerprint": str(entry.get("fingerprint") or ""),
        "candidates": candidates if isinstance(candidates, list) else [],
        "artist_id": entry.get("artist_id") or "",
        "catalog_ids": (list(catalog_ids) if isinstance(catalog_ids, list)
                        else None),
    }


def save_kind(kind: str, *, artists: dict, complete: bool,
              hidden_signature: str = ""):
    data = load()
    kinds = data.setdefault("kinds", {})
    now = time.time()
    kinds[kind] = {
        "updated_at": now,
        "complete": bool(complete),
        "hidden_signature": str(hidden_signature or ""),
        "artists": {
            str(name): _clean_artist_state(entry)
            for name, entry in (artists or {}).items()
        },
    }
    data["updated_at"] = now
    data["version"] = STATE_VERSION
    _write_state(data)
