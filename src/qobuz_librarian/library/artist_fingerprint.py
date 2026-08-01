"""Cheap local fingerprint for an artist folder.

The library refresh uses this to skip unchanged artist folders after a full
baseline exists. It deliberately uses only local file facts, not audio parsing,
so the check stays fast on large libraries and network mounts.
"""
import hashlib
import os
import stat
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.release_identity import (
    MANIFEST_NAME,
    ReleaseManifestError,
    read_release_identity,
)
from qobuz_librarian.library.scanner import iter_tree_no_symlinks


def _manifest_rows(artist_dir: Path) -> list[tuple]:
    """Describe every release manifest without following filesystem links."""
    rows = []

    def _raise(error):
        raise error

    for dirpath, _dirnames, filenames in os.walk(
            artist_dir, followlinks=False, onerror=_raise):
        if MANIFEST_NAME not in filenames:
            continue
        album_dir = Path(dirpath)
        manifest = album_dir / MANIFEST_NAME
        rel = manifest.relative_to(artist_dir).as_posix()
        try:
            before = os.stat(manifest, follow_symlinks=False)
        except OSError as exc:
            rows.append((rel, "unreadable", type(exc).__name__, exc.errno))
            continue
        if not stat.S_ISREG(before.st_mode):
            rows.append((
                rel,
                "invalid",
                "non-regular",
                int(before.st_mode),
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_mtime_ns),
                int(before.st_size),
            ))
            continue
        try:
            identity = read_release_identity(album_dir)
            if identity is None:
                state = ("changed", "missing-during-read")
            else:
                state = ("valid", identity.provider, identity.release_id)
        except (ReleaseManifestError, OSError) as exc:
            state = ("invalid", type(exc).__name__, exc.errno)
        try:
            after = os.stat(manifest, follow_symlinks=False)
        except OSError as exc:
            rows.append((rel, "changed", type(exc).__name__, exc.errno))
            continue
        metadata = (
            int(after.st_mode),
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_mtime_ns),
            int(after.st_size),
        )
        before_identity = (int(before.st_dev), int(before.st_ino))
        after_identity = (int(after.st_dev), int(after.st_ino))
        if before_identity != after_identity or not stat.S_ISREG(after.st_mode):
            state = ("changed",)
        rows.append((rel, *state, *metadata))
    return rows


def artist_fingerprint(artist_dir: Path) -> str:
    """Return a stable signature for audio and release identity state."""
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
        h.update(b"audio\0")
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(str(mtime_ns).encode("ascii"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\0")
    for row in sorted(_manifest_rows(artist_dir)):
        h.update(b"manifest\0")
        for value in row:
            h.update(str(value).encode("utf-8", "surrogateescape"))
            h.update(b"\0")
    return h.hexdigest()
