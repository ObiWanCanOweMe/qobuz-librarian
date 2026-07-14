"""Queue item construction."""


def _build_queue_item(*, album, album_dir, label, missing, present,
                      upgrade_only, auto_upgrade,
                      siblings_to_delete=None, quality=None,
                      force_track_by_track=False):
    """Bundle a confirmed download decision for batch processing.
    siblings_to_delete: list of sibling album dirs to remove after this item
    lands successfully.
    """
    return {
        "album": album,
        "album_dir": album_dir,
        "label": label,
        "missing": missing,
        "present": present,
        "upgrade_only": upgrade_only,
        "auto_upgrade": auto_upgrade,
        "backup_path": None,
        "snapshot_before": None,
        "n_ok": 0,
        "n_fail": 0,
        "n_lossy": 0,
        "failed_tracks": [],
        "lossy_tracks": [],
        "broken_tracks": [],
        "elapsed": 0.0,
        "imported": False,
        "result": None,
        "siblings_to_delete": list(siblings_to_delete or []),
        "quality": quality,
        "force_track_by_track": bool(force_track_by_track),
    }
