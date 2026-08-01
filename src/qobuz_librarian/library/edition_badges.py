"""Pure display labels for confusable Qobuz release editions."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from qobuz_librarian.library.release_identity import normalise_release_id
from qobuz_librarian.library.tags import normalize, strip_album_decorations

_EDITION_MARKER = re.compile(
    r"(?ix)\b("
    r"(?:\d{4}\s+)?remaster(?:ed)?|"
    r"deluxe(?:\s+edition)?|expanded(?:\s+edition)?|"
    r"special(?:\s+edition)?|collector(?:'s)?(?:\s+edition)?|"
    r"(?:\d{1,3}(?:st|nd|rd|th)\s+)?anniversary(?:\s+edition)?"
    r")\b"
)
_ORIGINAL_DATE = re.compile(r"(\d{4})(?:-(\d{2})-(\d{2}))?\Z")


def _original_release_year(album):
    value = album.get("release_date_original")
    if not isinstance(value, str):
        return None
    match = _ORIGINAL_DATE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day = match.groups()
    try:
        if month is None:
            datetime(int(year), 1, 1)
        else:
            datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return year


def edition_family_key(album):
    """Return the stable work-level key for a valid catalogue album."""
    if not isinstance(album, dict):
        return None
    artist_value = album.get("artist")
    title_value = album.get("title")
    if not isinstance(artist_value, dict) or not isinstance(title_value, str):
        return None
    artist_name = artist_value.get("name")
    if not isinstance(artist_name, str):
        return None

    artist = normalize(artist_name)
    title = normalize(strip_album_decorations(title_value))
    original_year = _original_release_year(album)
    return (artist, title, original_year) if artist and title and original_year else None


def edition_label(album):
    """Return a title's visible edition marker or its standard-edition label."""
    title = album.get("title") if isinstance(album, dict) else ""
    if not isinstance(title, str):
        return "Standard Edition"
    match = _EDITION_MARKER.search(title)
    return " ".join(match.group(1).split()) if match else "Standard Edition"


def _publication_year(album):
    released_at = album.get("released_at")
    if isinstance(released_at, bool) or not isinstance(released_at, (int, float)):
        return None
    if not math.isfinite(released_at):
        return None
    try:
        return str(datetime.fromtimestamp(released_at, tz=timezone.utc).year)
    except (ValueError, OSError, OverflowError):
        return None


def _track_count(album):
    count = album.get("tracks_count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return None
    return f"{count} tracks"


def _quality(album):
    bit_depth = album.get("maximum_bit_depth")
    sample_rate = album.get("maximum_sampling_rate")
    values = (bit_depth, sample_rate)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return None

    # Imported lazily so catalog.py can consume this module without a cycle.
    from qobuz_librarian.library.catalog import album_quality_label

    return album_quality_label(album).removesuffix(" (hi-res)")


def _canonical_album(album, release_id):
    """Compare duplicate rows after normalizing their one identity field."""
    return {**album, "id": release_id}


def _distinct_catalogue_rows(albums):
    rows = {}
    comparable = {}
    conflicts = set()
    for album in albums:
        if not isinstance(album, dict):
            continue
        release_id = normalise_release_id(album.get("id"))
        if release_id is None:
            continue
        candidate = _canonical_album(album, release_id)
        if release_id not in rows:
            rows[release_id] = album
            comparable[release_id] = candidate
        elif comparable[release_id] != candidate:
            conflicts.add(release_id)
    return {
        release_id: album
        for release_id, album in rows.items()
        if release_id not in conflicts
    }


def _ambiguous_groups(records, signatures):
    groups = defaultdict(list)
    for release_id, _album in records:
        groups[signatures[release_id]].append(release_id)
    return [release_ids for release_ids in groups.values() if len(release_ids) > 1]


def _add_minimum_differentiators(records, labels):
    albums = dict(records)
    components = {release_id: [label] for release_id, label in labels.items()}
    signatures = {
        release_id: (normalize(label),) for release_id, label in labels.items()
    }

    for value_from_album in (_publication_year, _track_count, _quality):
        for release_ids in _ambiguous_groups(records, signatures):
            values = {
                release_id: value_from_album(albums[release_id])
                for release_id in release_ids
            }
            candidate_signatures = {
                signatures[release_id] + ((values[release_id],) if values[release_id] else ())
                for release_id in release_ids
            }
            if len(candidate_signatures) < 2:
                continue
            for release_id, value in values.items():
                if value:
                    components[release_id].append(value)
                    signatures[release_id] += (value,)
    return components


def build_edition_badges(albums):
    """Build deterministic badges for every member of multi-release families."""
    distinct = _distinct_catalogue_rows(albums)
    families = defaultdict(list)
    for release_id, album in distinct.items():
        family_key = edition_family_key(album)
        if family_key is not None:
            families[family_key].append((release_id, album))

    badges = {}
    for records in families.values():
        if len(records) < 2:
            continue
        records.sort(key=lambda item: item[0])
        labels = {release_id: edition_label(album) for release_id, album in records}
        components = _add_minimum_differentiators(records, labels)
        for release_id, _album in records:
            badges[release_id] = " · ".join(
                [*components[release_id], f"Qobuz {release_id}"]
            )
    return dict(sorted(badges.items()))
