"""Exact, read-only library census persisted as a small derived snapshot."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from qobuz_librarian import config as cfg
from qobuz_librarian.library import flac_cache, scanner
from qobuz_librarian.library.release_identity import is_release_manifest_name

STATE_VERSION = 1


@dataclass
class InventoryResult:
    data: dict | None
    complete: bool
    processed: int
    errors: list[str]
    cancelled: bool = False


def _empty():
    return {
        "version": STATE_VERSION,
        "tiers": {
            "cd": [0, 0],
            "hires96": [0, 0],
            "hires192": [0, 0],
            "unknown": [0, 0],
        },
        "total_tracks": 0,
        "total_bytes": 0,
        "top_hires_artists": [],
        "reclaim_bytes": 0,
    }


def _tier(meta):
    bits = int((meta or {}).get("bits") or 0)
    sample_rate = int((meta or {}).get("sample_rate") or 0)
    if bits <= 0 or sample_rate <= 0:
        return "unknown", bits, sample_rate
    if bits <= 16:
        return "cd", bits, sample_rate
    if sample_rate <= 96000:
        return "hires96", bits, sample_rate
    return "hires192", bits, sample_rate


def build(
    root: Path | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_file: Callable[[int, Path], None] | None = None,
) -> InventoryResult:
    root = Path(root or cfg.MUSIC_ROOT)
    if not root.is_dir():
        return InventoryResult(None, False, 0, [f"{root} is not a readable directory"])
    data = _empty()
    errors = []
    hires_by_artist = {}
    processed = 0
    exts = set(cfg.AUDIO_EXTS)

    try:
        entries = scanner.iter_tree_no_symlinks(root, errors=errors)
        for path in entries:
            if cancel_check is not None and cancel_check():
                return InventoryResult(
                    None, False, processed, [str(error) for error in errors], True)
            if (
                is_release_manifest_name(path.name)
                or path.name.startswith("._")
                or path.suffix.lower() not in exts
            ):
                continue
            try:
                if not path.is_file():
                    continue
                sig = flac_cache.signature(path)
                if sig is None:
                    errors.append(f"could not stat {path}")
                    continue
                meta = scanner.read_audio_meta(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue

            processed += 1
            tier, _bits, sample_rate = _tier(meta)
            size = int((meta or {}).get("size") or sig[1] or 0)
            data["tiers"][tier][0] += 1
            data["tiers"][tier][1] += size
            data["total_tracks"] += 1
            data["total_bytes"] += size
            if tier in ("hires96", "hires192"):
                target = 44100 if sample_rate % 44100 == 0 else 48000
                if sample_rate > target:
                    data["reclaim_bytes"] += int(size * (1 - target / sample_rate))
                try:
                    artist = path.relative_to(root).parts[0]
                except (ValueError, IndexError):
                    artist = ""
                if artist:
                    hires_by_artist[artist] = hires_by_artist.get(artist, 0) + size
            if on_file is not None:
                on_file(processed, path)
    except OSError as exc:
        errors.append(f"{root}: {exc}")

    data["top_hires_artists"] = sorted(
        hires_by_artist.items(), key=lambda item: -item[1])[:5]
    return InventoryResult(
        data if not errors else None,
        not errors,
        processed,
        [str(error) for error in errors],
    )


def save(data):
    path = cfg.LIBRARY_CENSUS_FILE
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".qobuz_library_census.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load():
    try:
        data = json.loads(cfg.LIBRARY_CENSUS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    tiers = data.get("tiers")
    tier_names = ("cd", "hires96", "hires192", "unknown")
    if not isinstance(tiers, dict) or set(tiers) != set(tier_names):
        return None
    if any(
        not isinstance(tiers[name], list)
        or len(tiers[name]) != 2
        or any(not isinstance(value, int) or value < 0 for value in tiers[name])
        for name in tier_names
    ):
        return None
    numeric = ("total_tracks", "total_bytes", "reclaim_bytes")
    if any(not isinstance(data.get(name), int) or data[name] < 0 for name in numeric):
        return None
    top = data.get("top_hires_artists")
    if not isinstance(top, list) or any(
        not isinstance(row, list)
        or len(row) != 2
        or not isinstance(row[0], str)
        or not isinstance(row[1], int)
        or row[1] < 0
        for row in top
    ):
        return None
    data["top_hires_artists"] = [(name, size) for name, size in top]
    return data
