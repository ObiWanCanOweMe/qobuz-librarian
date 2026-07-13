from argparse import Namespace

from qobuz_librarian.completion import (
    CompletionOrigin,
    CompletionOriginKind,
    DownloadCounts,
    DownloadCoverage,
    ManagedImportEvidence,
    ManagedMapping,
    RecoveryOwner,
    SourceTransitionKind,
    StagedBinding,
)
from qobuz_librarian.queue.durable_album import (
    advance_completion_sources,
    completion_input_from_download,
    initial_completion_input,
    managed_binding_records,
    managed_completion_input,
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


def test_durable_input_tracks_controlled_same_path_rewrites():
    album = _album()
    plan = plan_durable_new_album(_item(album), Namespace(no_import=False))
    owner = RecoveryOwner("a" * 64, "b" * 64)
    initial = initial_completion_input(
        plan,
        owner,
        CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
    )
    slots = plan.expectation.catalogue_slots
    downloaded = tuple(StagedBinding(
        slot,
        f"/staging/{index}.flac",
        (1, index, 0o100600, 10, 20, 30),
    ) for index, slot in enumerate(slots, start=1))
    ready = completion_input_from_download(initial, DownloadCoverage(
        album_id="42",
        catalogue_slots=slots,
        requested_slots=slots,
        bindings=downloaded,
        counts=DownloadCounts(),
    ))
    rewritten = tuple(StagedBinding(
        binding.slot,
        binding.path,
        (*binding.identity[:1], binding.identity[1] + 10, *binding.identity[2:]),
    ) for binding in downloaded)
    lyric_ready = advance_completion_sources(
        ready,
        rewritten,
        SourceTransitionKind.LYRICS_TAG,
    )
    assert lyric_ready is not None
    assert all(
        lineage.transitions[-1].kind is SourceTransitionKind.LYRICS_TAG
        for lineage in lyric_ready.lineages
    )

    mappings = tuple(ManagedMapping(
        binding.slot,
        binding.path,
        (*binding.identity[:1], binding.identity[1] + 10, *binding.identity[2:]),
        f"Artist/Album/{index}.flac",
        (2, index, 10, 20, 30),
    ) for index, binding in enumerate(rewritten, start=1))
    managed = ManagedImportEvidence(
        owner=owner,
        library_root="/music",
        library_root_identity=(2, 1, 0, 0, 0),
        album_path="Artist/Album",
        album_identity=(2, 2, 0, 0, 0),
        manifest_hash="c" * 64,
        mappings=mappings,
    )
    beets_ready = managed_completion_input(lyric_ready, managed)
    assert beets_ready is not None
    assert all(
        lineage.transitions[-1].kind is SourceTransitionKind.BEETS_TAG_CLEAN
        for lineage in beets_ready.lineages
    )
    assert managed_binding_records(beets_ready) == tuple({
        "slot": lineage.slot,
        "path": lineage.current.path,
        "identity": list(lineage.current.identity),
    } for lineage in beets_ready.lineages)
