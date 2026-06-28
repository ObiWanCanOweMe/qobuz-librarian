"""Cheap local fingerprint for an artist folder.

The library refresh uses this to skip unchanged artist folders after a full
baseline exists. It deliberately uses only local file facts, not audio parsing,
so the check stays fast on large libraries and network mounts.
"""
import hashlib
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.scanner import iter_tree_no_symlinks


def artist_fingerprint(artist_dir: Path) -> str:
    """Return a stable signature for audio files under ``artist_dir``."""
    h = hashlib.sha256()
    exts = set(cfg.AUDIO_EXTS)
    rows: list[tuple[str, int, int]] = []

    for path in iter_tree_no_symlinks(artist_dir):
        if path.suffix.lower() not in exts:
            continue
        try:
            if not path.is_file():
                continue
            st = path.stat()
            rel = path.relative_to(artist_dir).as_posix()
            rows.append((rel, int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            rel = path.name
            try:
                rel = path.relative_to(artist_dir).as_posix()
            except ValueError:
                pass
            rows.append((rel, -1, -1))

    for rel, mtime_ns, size in sorted(rows):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(str(mtime_ns).encode("ascii"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()
