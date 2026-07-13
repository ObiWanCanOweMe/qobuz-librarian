import copy
from dataclasses import replace

import pytest

from qobuz_librarian.completion import (
    AlbumInventory,
    BaselineReceipt,
    CompletionExpectation,
    CompletionFailure,
    CompletionInput,
    CompletionOrigin,
    CompletionOriginKind,
    CompletionScope,
    DownloadCounts,
    DownloadCoverage,
    LandingReceipt,
    ManagedImportEvidence,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    SourceTransition,
    SourceTransitionKind,
    StagedBinding,
    StagedReceipt,
    TrackQuality,
    assess_completion,
    completion_input_extends,
    completion_input_ready,
    parse_completion_input_record,
    parse_completion_record,
)


def test_completion_input_is_canonical_owner_bound_and_append_only():
    owner = RecoveryOwner("a" * 64, "b" * 64)
    slots = ("qobuz:101", "qobuz:202")
    expectation = CompletionExpectation(
        album_id="42",
        scope=CompletionScope.ALBUM,
        catalogue_slots=slots,
        requested_slots=slots,
        quality_targets=(
            QualityTarget(slots[0], 24, 96_000),
            QualityTarget(slots[1], 16, 44_100),
        ),
    )
    initial = CompletionInput(
        owner=owner,
        origin=CompletionOrigin(CompletionOriginKind.WEB_JOB, "abcd1234"),
        expectation=expectation,
        effective_tier=4,
    )
    first = StagedReceipt(
        "/staging/01.flac", (1, 11, 0o100644, 100, 1_000, 2_000))
    first_tagged = StagedReceipt(
        first.path, (1, 11, 0o100644, 120, 1_100, 2_100))
    second = StagedReceipt(
        "/staging/02.flac", (1, 22, 0o100644, 200, 1_200, 2_200))
    complete = replace(
        initial,
        lineages=(
            SourceLineage(slots[0], first, (SourceTransition(
                SourceTransitionKind.BEETS_TAG_CLEAN,
                first,
                first_tagged,
            ),)),
            SourceLineage(slots[1], second),
        ),
        counts=DownloadCounts(),
    )
    record = complete.to_record()

    assert parse_completion_input_record(
        record, expected_owner=owner) == complete
    assert completion_input_extends(initial, complete)
    assert completion_input_ready(complete)
    assert not completion_input_extends(
        initial, replace(complete, effective_tier=2))
    assert parse_completion_input_record(
        record,
        expected_owner=RecoveryOwner(owner.operation_id, "c" * 64),
    ) is None

    discontinuous = copy.deepcopy(record)
    discontinuous["download"]["lineages"][0]["transitions"][0][
        "before"
    ]["identity"][1] = 999
    assert parse_completion_input_record(discontinuous) is None

    noncanonical = copy.deepcopy(record)
    noncanonical["download"]["lineages"].reverse()
    assert parse_completion_input_record(noncanonical) is None


@pytest.mark.parametrize(
    ("case", "expected_failure"),
    [
        ("exact", None),
        ("same-count-wrong-slot", CompletionFailure.DOWNLOAD_INCOMPLETE),
        ("duplicate-source-identity", CompletionFailure.DOWNLOAD_INCOMPLETE),
        ("extra-unmatched-audio", CompletionFailure.DOWNLOAD_REMAINDER),
        ("lossy-broken-remainder", CompletionFailure.DOWNLOAD_REMAINDER),
        ("unknown-quality", CompletionFailure.QUALITY_MISMATCH),
        ("quality-other-destination", CompletionFailure.QUALITY_MISMATCH),
        ("fabricated-low-target", CompletionFailure.QUALITY_MISMATCH),
        ("split-destination", CompletionFailure.DESTINATION_MISMATCH),
        ("requested-with-baseline", None),
        ("requested-only", None),
        ("requested-outside-album", CompletionFailure.DESTINATION_MISMATCH),
        ("noncanonical-target-order", CompletionFailure.QUALITY_MISMATCH),
        ("ambiguous-slot", CompletionFailure.AMBIGUOUS_SLOT),
        ("unresolved-recovery", CompletionFailure.RECOVERY_UNRESOLVED),
        ("uncertain-managed-import", CompletionFailure.MANAGED_IMPORT_REQUIRED),
    ],
)
def test_exact_completion_requires_closed_slot_evidence(case, expected_failure):
    owner = RecoveryOwner("a" * 64, "b" * 64)
    expectation = CompletionExpectation(
        album_id="42",
        scope=CompletionScope.ALBUM,
        catalogue_slots=("qobuz:101", "qobuz:202", "qobuz:303"),
        requested_slots=("qobuz:101", "qobuz:202"),
        quality_targets=(
            QualityTarget("qobuz:101", 24, 96_000),
            QualityTarget("qobuz:202", 24, 96_000),
            QualityTarget("qobuz:303", 24, 96_000),
        ),
        baseline_slots=("qobuz:303",),
    )
    first_source = (1, 11, 0o100644, 100, 1_000, 2_000)
    second_source = (1, 22, 0o100644, 200, 1_100, 2_100)
    first_destination = (2, 111, 100, 1_000, 3_000)
    second_destination = (2, 222, 200, 1_100, 3_100)
    download = DownloadCoverage(
        album_id="42",
        catalogue_slots=expectation.catalogue_slots,
        requested_slots=expectation.requested_slots,
        # Reversed output order and identical titles are deliberate: identity
        # must follow the Qobuz slot, never zip/order/title coincidence.
        bindings=(
            StagedBinding("qobuz:202", "/staging/disc2/song.flac", second_source),
            StagedBinding("qobuz:101", "/staging/disc1/song.flac", first_source),
        ),
        counts=DownloadCounts(),
    )
    managed = ManagedImportEvidence(
        owner=owner,
        library_root="/music",
        library_root_identity=(2, 10, 0, 1_000, 2_000),
        album_path="Artist/Album",
        album_identity=(2, 99, 0, 900, 2_900),
        manifest_hash="c" * 64,
        mappings=(
            ManagedMapping(
                "qobuz:202",
                "/staging/disc2/song.flac",
                second_source,
                "Artist/Album/Disc 2/01 Song.flac",
                second_destination,
            ),
            ManagedMapping(
                "qobuz:101",
                "/staging/disc1/song.flac",
                first_source,
                "Artist/Album/Disc 1/01 Song.flac",
                first_destination,
            ),
        ),
    )
    landings = (
        LandingReceipt(
            "qobuz:202",
            "Artist/Album/Disc 2/01 Song.flac",
            second_destination,
            "d" * 64,
        ),
        LandingReceipt(
            "qobuz:101",
            "Artist/Album/Disc 1/01 Song.flac",
            first_destination,
            "e" * 64,
        ),
    )
    baseline = (
        BaselineReceipt(
            "qobuz:303",
            "Artist/Album/Disc 2/02 Other.flac",
            (2, 333, 300, 1_200, 3_200),
            "f" * 64,
        ),
    )
    inventory = AlbumInventory(
        path="Artist/Album",
        identity=(2, 99, 0, 900, 2_900),
        audio=landings + baseline,
    )
    quality = tuple(
        TrackQuality(
            slot=receipt.slot,
            path=receipt.path,
            identity=receipt.identity,
            sha256=receipt.sha256,
            served_bits=24,
            served_rate=96_000,
        )
        for receipt in landings + baseline
    )
    recovery_references = ()

    if case == "same-count-wrong-slot":
        download = replace(download, bindings=(
            replace(download.bindings[0], slot="qobuz:303"),
            download.bindings[1],
        ))
    elif case == "duplicate-source-identity":
        download = replace(download, bindings=(
            download.bindings[0],
            replace(download.bindings[1], identity=second_source),
        ))
    elif case == "extra-unmatched-audio":
        download = replace(
            download, counts=DownloadCounts(unmatched=1, extra=1))
    elif case == "lossy-broken-remainder":
        download = replace(
            download, counts=DownloadCounts(lossy=1, broken=1))
    elif case == "unknown-quality":
        quality = (
            quality[0],
            quality[1],
            replace(quality[2], served_bits=0, served_rate=0),
        )
    elif case == "quality-other-destination":
        quality = (
            replace(
                quality[0],
                path="Artist/Album/Disc 2/99 Other.flac",
            ),
            *quality[1:],
        )
    elif case == "fabricated-low-target":
        quality = (
            replace(quality[0], served_bits=1, served_rate=1),
            *quality[1:],
        )
    elif case == "split-destination":
        split_path = "Artist/Other Album/Disc 2/01 Song.flac"
        managed = replace(managed, mappings=(
            replace(managed.mappings[0], destination_path=split_path),
            managed.mappings[1],
        ))
        landings = (
            replace(landings[0], path=split_path),
            landings[1],
        )
        inventory = replace(
            inventory,
            path="Artist",
            audio=landings + baseline,
        )
    elif case == "requested-with-baseline":
        expectation = replace(
            expectation,
            scope=CompletionScope.REQUESTED,
            quality_targets=expectation.quality_targets[:2],
        )
        quality = quality[:2]
    elif case in ("requested-only", "requested-outside-album"):
        expectation = replace(
            expectation,
            scope=CompletionScope.REQUESTED,
            quality_targets=expectation.quality_targets[:2],
            baseline_slots=(),
        )
        baseline = ()
        inventory = None
        quality = quality[:2]
        if case == "requested-outside-album":
            outside = "Artist/Other Album/Disc 2/01 Song.flac"
            managed = replace(managed, mappings=(
                replace(managed.mappings[0], destination_path=outside),
                managed.mappings[1],
            ))
            landings = (
                replace(landings[0], path=outside),
                landings[1],
            )
            quality = (
                replace(quality[0], path=outside),
                quality[1],
            )
    elif case == "noncanonical-target-order":
        expectation = replace(
            expectation,
            quality_targets=tuple(reversed(expectation.quality_targets)),
        )
    elif case == "ambiguous-slot":
        expectation = replace(
            expectation,
            catalogue_slots=("qobuz:101", "qobuz:202", "album-slot:3"),
        )
    elif case == "unresolved-recovery":
        recovery_references = ({"kind": "backup"},)
    elif case == "uncertain-managed-import":
        managed = None

    assessment = assess_completion(
        owner=owner,
        expectation=expectation,
        download=download,
        managed=managed,
        landings=landings,
        baseline=baseline,
        inventory=inventory,
        quality=quality,
        recovery_references=recovery_references,
    )

    assert assessment.failure is expected_failure
    assert (assessment.evidence is not None) is (expected_failure is None)
    if assessment.evidence is not None:
        if assessment.evidence.inventory is not None:
            assert tuple(
                receipt.slot for receipt in assessment.evidence.inventory.audio
            ) == expectation.catalogue_slots
        record = assessment.evidence.to_record()
        assert parse_completion_record(
            record,
            expected_owner=owner,
            expected_expectation=expectation,
            expected_download=download,
        ) == assessment.evidence
        if case == "exact":
            assert parse_completion_record(
                record,
                expected_owner=owner,
                expected_expectation=replace(
                    expectation,
                    quality_targets=(
                        replace(expectation.quality_targets[0], bits=16),
                        *expectation.quality_targets[1:],
                    ),
                ),
                expected_download=download,
            ) is None
            assert parse_completion_record(
                record,
                expected_owner=owner,
                expected_expectation=expectation,
                expected_download=replace(
                    download,
                    bindings=(
                        replace(
                            download.bindings[0],
                            path="/staging/other.flac",
                        ),
                        *download.bindings[1:],
                    ),
                ),
            ) is None
            record["unexpected"] = "refuse"
            assert parse_completion_record(
                record,
                expected_owner=owner,
                expected_expectation=expectation,
                expected_download=download,
            ) is None
