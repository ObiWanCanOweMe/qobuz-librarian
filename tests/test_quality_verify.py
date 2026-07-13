import json
from pathlib import Path

import pytest

import qobuz_librarian.config as config
from qobuz_librarian.quality import verify


class _FakeDir:
    pass


def _receipts(*paths):
    from qobuz_librarian.integrations.staging import capture_file

    return [receipt for path in paths if (receipt := capture_file(path))]


def _patch(monkeypatch, tracks, tier):
    monkeypatch.setattr(verify, "read_album_dir", lambda d: tracks)
    monkeypatch.setattr(config, "STREAMRIP_QUALITY", tier)


def test_cd_only_source_served_cd_is_not_under(monkeypatch):
    _patch(monkeypatch, [{"bits": 16, "sample_rate": 44100}], tier=4)
    album = {"maximum_bit_depth": 16, "maximum_sampling_rate": 44.1}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is False


@pytest.mark.parametrize(
    ("served", "album"),
    [
        ((16, 44_100), (24, 96.0)),
        ((24, 44_100), (16, 96.0)),
    ],
)
def test_hires_source_served_below_one_dimension_is_under(
        monkeypatch, served, album):
    _patch(monkeypatch, [{
        "bits": served[0],
        "sample_rate": served[1],
    }], tier=4)
    album = {
        "maximum_bit_depth": album[0],
        "maximum_sampling_rate": album[1],
    }
    r = verify.rip_shortfall([_FakeDir()], album)
    assert r["under"] is True and r["n_below"] == 1


def test_one_low_track_among_good_is_under(monkeypatch):
    _patch(monkeypatch, [
        {"bits": 24, "sample_rate": 96000},
        {"bits": 16, "sample_rate": 44100},
        {"bits": 24, "sample_rate": 96000},
    ], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    r = verify.rip_shortfall([_FakeDir()], album)
    assert r["under"] is True and r["n_below"] == 1


def test_served_at_cap_target_is_not_under(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=3)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is False


def test_cap_matters_same_rip_under_higher_cap(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    assert verify.rip_shortfall([_FakeDir()], album)["under"] is True


def test_effective_tier_override_controls_target(monkeypatch):
    _patch(monkeypatch, [{"bits": 24, "sample_rate": 96000}], tier=4)
    album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
    result = verify.rip_shortfall([_FakeDir()], album, effective_tier=3)
    assert result["under"] is False


def test_retry_coverage_rejects_wrong_or_duplicate_same_count_tracks():
    original = {
        "_expected_track_keys": ["qobuz:1", "qobuz:2"],
        "_clean_track_keys": ["qobuz:1", "qobuz:2"],
    }
    wrong = {
        "_expected_track_keys": ["qobuz:1", "qobuz:2"],
        "_clean_track_keys": ["qobuz:1", "qobuz:3"],
        "_exact_track_coverage": True,
        "_unmatched_audio": 0,
    }
    duplicate = {
        "_expected_track_keys": ["qobuz:1", "qobuz:2"],
        "_clean_track_keys": ["qobuz:1", "qobuz:1"],
        "_exact_track_coverage": True,
        "_unmatched_audio": 0,
    }

    assert verify.retry_preserves_track_coverage(original, wrong) is False
    assert verify.retry_preserves_track_coverage(original, duplicate) is False


def test_redownload_fallback_restores_first_staged_rip_when_retry_lands_nothing(
        monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    dirs, retry_kept = verify.redownload_with_staged_fallback(
        [first],
        run_retry=lambda: None,
        collect_staged_dirs=lambda: [],
        collect_retry_files=lambda: [],
        retry_preserves_original=lambda: False)

    assert dirs == [first]
    assert retry_kept is False
    assert (first / "01.flac").read_bytes() == b"first rip"


def test_redownload_fallback_restores_first_rip_when_same_count_retry_is_wrong(
        monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    def run_retry():
        first.mkdir(parents=True)
        (first / "02.flac").write_bytes(b"retry rip")

    dirs, retry_kept = verify.redownload_with_staged_fallback(
        [first],
        run_retry=run_retry,
        collect_staged_dirs=lambda: [first],
        collect_retry_files=lambda: _receipts(first / "02.flac"),
        retry_preserves_original=lambda: False)

    assert dirs == [first]
    assert retry_kept is False
    assert (first / "01.flac").read_bytes() == b"first rip"
    assert not (first / "02.flac").exists()


def test_redownload_fallback_restores_first_rip_on_interrupt(
        monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    (first / "01.flac").write_bytes(b"first rip")
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    def run_retry():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        verify.redownload_with_staged_fallback(
            [first],
            run_retry=run_retry,
            collect_staged_dirs=lambda: [],
            collect_retry_files=lambda: [],
            retry_preserves_original=lambda: False)

    assert (first / "01.flac").read_bytes() == b"first rip"


def test_redownload_fallback_restores_first_rip_when_retry_less_complete(
        monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    for n in ("01", "02", "03"):
        (first / f"{n}.flac").write_bytes(b"first rip")
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    def run_retry():
        first.mkdir(parents=True, exist_ok=True)
        (first / "01.flac").write_bytes(b"retry rip")

    dirs, retry_kept = verify.redownload_with_staged_fallback(
        [first],
        run_retry=run_retry,
        collect_staged_dirs=lambda: [first],
        collect_retry_files=lambda: _receipts(first / "01.flac"),
        retry_preserves_original=lambda: False)

    assert dirs == [first]
    assert retry_kept is False
    assert sorted(p.name for p in first.iterdir()) == [
        "01.flac", "02.flac", "03.flac"]
    assert (first / "01.flac").read_bytes() == b"first rip"


def test_redownload_fallback_preserves_unrelated_retry_occupant(
        monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    first = staging / "Artist" / "Album"
    first.mkdir(parents=True)
    for n in ("01", "02"):
        (first / f"{n}.flac").write_bytes(b"first rip")

    monkeypatch.setattr(config, "STAGING_DIR", staging)

    def run_retry():
        first.mkdir(parents=True)
        (first / "01.flac").write_bytes(b"retry rip")
        (first / "unrelated.txt").write_bytes(b"do not touch")

    with pytest.raises(OSError):
        verify.redownload_with_staged_fallback(
            [first],
            run_retry=run_retry,
            collect_staged_dirs=lambda: [first],
            collect_retry_files=lambda: _receipts(first / "01.flac"),
            retry_preserves_original=lambda: False)

    assert (first / "unrelated.txt").read_bytes() == b"do not touch"
    assert any(
        path.read_bytes() == b"first rip"
        for path in (staging / config.BEETS_RETRY_DIR).rglob("*.flac")
    )


def test_staging_park_rolls_back_a_move_interrupted_before_receipt(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import staging as staging_tx

    owner = {
        "operation_id": "a" * 64,
        "item_id": "b" * 64,
    }
    staging = tmp_path / "staging"
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    def failed_checkpoint(_record):
        raise OSError("journal checkpoint failed")

    with pytest.raises(OSError, match="journal checkpoint failed"):
        staging_tx.create_staging_run(
            owner=owner, on_created=failed_checkpoint)
    assert not list(staging.glob(".qobuz-run-*"))

    created = []
    run = staging_tx.create_staging_run(
        owner=owner, on_created=created.append)
    assert created == [run.to_record()]
    assert staging_tx.staging_run_from_record(created[0]) == run
    first = run.path / "Artist" / "First"
    second = run.path / "Artist" / "Second"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "01.flac").write_bytes(b"first")
    (second / "01.flac").write_bytes(b"second")

    real_move = staging_tx._move_tree
    calls = 0
    intents = []

    def interrupt_after_move(receipt, destination, destination_name):
        nonlocal calls
        calls += 1
        moved = real_move(receipt, destination, destination_name)
        if calls == 1:
            raise KeyboardInterrupt
        return moved

    monkeypatch.setattr(staging_tx, "_move_tree", interrupt_after_move)

    with pytest.raises(KeyboardInterrupt):
        staging_tx.park_trees(
            [first, second],
            "quality",
            kind="quality",
            owner=owner,
            on_intent=intents.append,
        )

    assert (first / "01.flac").read_bytes() == b"first"
    assert (second / "01.flac").read_bytes() == b"second"
    retry_root = staging / config.BEETS_RETRY_DIR
    assert not list(retry_root.iterdir())
    assert len(intents) == 1

    monkeypatch.setattr(staging_tx, "_move_tree", real_move)
    group = staging_tx.park_trees(
        [first, second],
        "quality",
        kind="quality",
        owner=owner,
        on_intent=lambda _intent: None,
    )
    assert group is not None
    manifest_path = group.path / staging_tx._MANIFEST
    manifest_text = manifest_path.read_text(encoding="utf-8")
    duplicate_tree = json.loads(manifest_text)
    duplicate_tree["trees"].append(duplicate_tree["trees"][0])
    manifest_path.write_text(json.dumps(duplicate_tree), encoding="utf-8")
    assert staging_tx.inspect_retry_group(group.path).status == "malformed"
    assert staging_tx.load_group(
        group.path, kind="quality", expected_owner=owner) is None
    assert (group.trees[0].path / "01.flac").read_bytes() == b"first"
    assert (group.trees[1].path / "01.flac").read_bytes() == b"second"

    manifest_path.write_text(manifest_text, encoding="utf-8")
    assert staging_tx.load_group(
        group.path, kind="quality", expected_owner=owner) == group
    wrong_owner = {
        "operation_id": "c" * 64,
        "item_id": "d" * 64,
    }
    assert staging_tx.load_group(
        group.path, kind="quality", expected_owner=wrong_owner) is None
    assert staging_tx.discard_group(
        group, expected_owner=wrong_owner) is False
    assert group.path.exists()
    assert staging_tx.discard_group(group, expected_owner=owner) is True

    rejected = run.path / "rejected.mp3"
    rejected.write_bytes(b"lossy")
    rejected_receipt = staging_tx.capture_file(rejected)
    file_intents = []
    assert staging_tx.quarantine_file(
        rejected_receipt,
        owner=owner,
        on_intent=file_intents.append,
    ) is not None
    assert len(file_intents) == 1
    assert file_intents[0]["owner"] == owner
    assert staging_tx.discard_quarantined_file(
        rejected_receipt, expected_owner=wrong_owner) is False
    assert staging_tx.discard_quarantined_file(
        rejected_receipt, expected_owner=owner) is True

    imported = run.path / "Imported"
    imported.mkdir()
    (imported / "01.flac").write_bytes(b"imported")
    imported_group = staging_tx.park_trees(
        [imported],
        "quality",
        kind="quality",
        owner=owner,
        on_intent=lambda _intent: None,
    )
    imported_tree = imported_group.trees[0]
    (imported_tree.path / "01.flac").unlink()
    stale_tree = staging_tx.TreeReceipt(
        imported_tree.path,
        "wrong/source",
        imported_tree.directories,
        imported_tree.files,
    )
    stale_group = staging_tx.ParkedGroup(
        imported_group.path, imported_group.kind, (stale_tree,), owner)
    assert staging_tx.reconcile_imported_group(
        stale_group, expected_owner=owner)["reason"] == "invalid"
    assert staging_tx.reconcile_imported_group(
        imported_group, expected_owner=wrong_owner)["reason"] == "owner"
    assert staging_tx.reconcile_imported_group(
        imported_group, expected_owner=owner) == {
            "moved_audio": True,
            "cleared": True,
            "reason": "",
        }

    partial_first = run.path / "Partial First"
    partial_second = run.path / "Partial Second"
    partial_first.mkdir()
    partial_second.mkdir()
    (partial_first / "01.flac").write_bytes(b"first")
    (partial_second / "01.flac").write_bytes(b"second")
    real_rollback = staging_tx._rollback_parked
    partial_intents = []
    partial_calls = 0

    def die_after_one_move(receipt, destination, destination_name):
        nonlocal partial_calls
        partial_calls += 1
        moved = real_move(receipt, destination, destination_name)
        if partial_calls == 1:
            raise SystemExit("simulated process death")
        return moved

    monkeypatch.setattr(staging_tx, "_move_tree", die_after_one_move)
    monkeypatch.setattr(staging_tx, "_rollback_parked", lambda *_: False)
    with pytest.raises(SystemExit, match="simulated process death"):
        staging_tx.park_trees(
            [partial_first, partial_second],
            "quality",
            kind="quality",
            owner=owner,
            on_intent=partial_intents.append,
        )
    partial_path = Path(partial_intents[0]["path"])
    inspection = staging_tx.inspect_retry_group(
        partial_path, expected_owner=owner)
    assert inspection.status == "incomplete"
    assert len(inspection.planned_trees) == 2
    assert sum(tree is not None for tree in inspection.current_trees) == 1
    from qobuz_librarian.integrations.rip import cleanup_staging_residue

    assert cleanup_staging_residue() == 0
    monkeypatch.setattr(staging_tx, "_rollback_parked", real_rollback)


def test_interrupted_run_manifest_precedes_move_and_survives_fatal_signal(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(config, "STAGING_DIR", staging)
    run = staging_tx.create_staging_run()
    disc = run.path / "Artist" / "Album" / "Disc 2"
    disc.mkdir(parents=True)
    (disc / "01.flac").write_bytes(b"downloaded audio")

    real_move = staging_tx._move_tree

    def interrupt_after_move(receipt, destination, destination_name):
        assert (destination / staging_tx._MANIFEST).is_file()
        real_move(receipt, destination, destination_name)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(staging_tx, "_move_tree", interrupt_after_move)

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        staging_tx.retain_staging_run(run)

    assert not run.path.exists()
    groups = staging_tx.list_groups(kind="interrupted")
    assert len(groups) == 1
    assert (groups[0].trees[0].path / "Artist" / "Album" / "Disc 2"
            / "01.flac").read_bytes() == b"downloaded audio"


def test_interrupted_run_retention_refuses_a_still_open_writer(
        monkeypatch, tmp_path):
    import os

    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(config, "STAGING_DIR", staging)
    run = staging_tx.create_staging_run()
    partial = run.path / "Artist" / "Album" / "01.flac"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial audio")
    writer = os.open(partial, os.O_WRONLY)
    try:
        assert staging_tx.retain_staging_run(run) is None
    finally:
        os.close(writer)

    assert partial.read_bytes() == b"partial audio"
    assert staging_tx.list_groups(kind="interrupted") == []


def test_rejected_file_manifest_lives_until_exact_disposal(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    rejected = staging / "run" / "bad.mp3"
    rejected.parent.mkdir(parents=True)
    rejected.write_bytes(b"not lossless")
    monkeypatch.setattr(config, "STAGING_DIR", staging)
    receipt = staging_tx.capture_file(rejected)

    assert staging_tx.quarantine_file(receipt, ".rejected") is not None
    groups = staging_tx.list_file_groups(kind="rejected")
    assert len(groups) == 1
    assert groups[0].retained.path.read_bytes() == b"not lossless"
    assert staging_tx.discard_quarantined_file(receipt) is True
    assert staging_tx.list_file_groups(kind="rejected") == []


def test_staging_restore_rolls_back_a_partial_multi_tree_restore(
        monkeypatch, tmp_path):
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    first = staging / "Artist" / "First"
    second = staging / "Artist" / "Second"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "01.flac").write_bytes(b"first")
    (second / "01.flac").write_bytes(b"second")
    monkeypatch.setattr(config, "STAGING_DIR", staging)

    group = staging_tx.park_trees(
        [first, second], "quality", kind="quality")
    assert group is not None

    real_move = staging_tx._move_tree
    calls = 0

    def fail_second_move(receipt, destination, destination_name):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_move(receipt, destination, destination_name)

    monkeypatch.setattr(staging_tx, "_move_tree", fail_second_move)

    with pytest.raises(KeyboardInterrupt):
        staging_tx.restore_group(group)
    assert not first.exists()
    assert not second.exists()
    reloaded = staging_tx.load_group(group.path, kind="quality")
    assert reloaded is not None
    assert sorted(
        (tree.path / "01.flac").read_bytes() for tree in reloaded.trees
    ) == [b"first", b"second"]
