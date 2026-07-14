from argparse import Namespace

from qobuz_librarian.completion import (
    CompletionOrigin,
    CompletionOriginKind,
    DownloadCounts,
    DownloadCoverage,
    RecoveryOwner,
    StagedBinding,
)
from qobuz_librarian.queue.durable_album import (
    completion_input_from_download,
    initial_completion_input,
    plan_durable_new_album,
)


def _album():
    return {
        "id": "42",
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96,
        "tracks": {"items": [
            {"id": "101", "media_number": 1, "track_number": 1},
            {"id": "202", "media_number": 1, "track_number": 2},
        ]},
    }


def _item(album):
    return {
        "album": album,
        "album_dir": None,
        "missing": album["tracks"]["items"],
        "present": [],
        "upgrade_only": False,
        "auto_upgrade": False,
        "force_track_by_track": False,
        "siblings_to_delete": [],
        "quality": 4,
    }


def test_durable_new_album_plan_refuses_existing_or_partial_work():
    album = _album()
    args = Namespace(no_import=False)
    assert plan_durable_new_album(_item(album), args) is not None

    existing = _item(album)
    existing["album_dir"] = "/music/Artist/Album"
    assert plan_durable_new_album(existing, args) is None

    partial = _item(album)
    partial["missing"] = partial["missing"][:1]
    assert plan_durable_new_album(partial, args) is None

    destructive = _item(album)
    destructive["siblings_to_delete"] = ["/music/old"]
    assert plan_durable_new_album(destructive, args) is None

    lossy = _album()
    lossy["maximum_bit_depth"] = 0
    assert plan_durable_new_album(_item(lossy), args) is None

    assert plan_durable_new_album(
        _item(album), Namespace(no_import=False, consolidate=True)
    ) is None
def test_durable_input_becomes_ready_only_for_exact_zero_remainder_download():
    album = _album()
    plan = plan_durable_new_album(_item(album), Namespace(no_import=False))
    owner = RecoveryOwner("a" * 64, "b" * 64)
    initial = initial_completion_input(
        plan,
        owner,
        CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
    )
    slots = plan.expectation.catalogue_slots
    bindings = tuple(
        StagedBinding(
            slot,
            f"/staging/{index}.flac",
            (1, index, 0o100600, 10, 20, 30),
        )
        for index, slot in enumerate(slots, start=1)
    )
    complete = DownloadCoverage(
        album_id="42",
        catalogue_slots=slots,
        requested_slots=slots,
        bindings=bindings,
        counts=DownloadCounts(),
    )
    assert completion_input_from_download(initial, complete) is not None

    incomplete = DownloadCoverage(
        album_id="42",
        catalogue_slots=slots,
        requested_slots=slots,
        bindings=bindings,
        counts=DownloadCounts(failed=1),
    )
    assert completion_input_from_download(initial, incomplete) is None


