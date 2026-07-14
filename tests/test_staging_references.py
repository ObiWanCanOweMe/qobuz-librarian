
import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import staging as staging_module
from qobuz_librarian.integrations.staging import (
    FileGroup,
    ParkedGroup,
    StagingReferenceStatus,
    capture_file,
    inspect_staging_group_reference,
    park_trees,
    quarantine_file,
)


def _owner(marker):
    return {
        "operation_id": marker * 64,
        "item_id": chr(ord(marker) + 1) * 64,
    }


@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", root)
    return root


def test_owned_staging_group_reference_requires_exact_manifested_contents(
        staging):
    owner = _owner("c")
    source = staging / "Artist - Album"
    source.mkdir(parents=True)
    track = source / "01.flac"
    track.write_bytes(b"original")
    records = []
    group = park_trees(
        [source],
        "failed import",
        owner=owner,
        on_intent=records.append,
    )
    assert group is not None

    matched = inspect_staging_group_reference(records[0], owner)
    assert matched.status is StagingReferenceStatus.MATCH
    assert isinstance(matched.evidence, ParkedGroup)

    parked_track = group.trees[0].path / "01.flac"
    parked_track.write_bytes(b"replacement")
    assert inspect_staging_group_reference(
        records[0], owner).status is StagingReferenceStatus.CHANGED

    wrong_owner = _owner("e")
    assert inspect_staging_group_reference(
        records[0], wrong_owner).status is StagingReferenceStatus.UNAVAILABLE

    loose = staging / "loose.flac"
    loose.write_bytes(b"audio")
    file_records = []
    file_group = quarantine_file(
        capture_file(loose),
        owner=owner,
        on_intent=file_records.append,
    )
    assert file_group is not None
    file_match = inspect_staging_group_reference(file_records[0], owner)
    assert file_match.status is StagingReferenceStatus.MATCH
    assert isinstance(file_match.evidence, FileGroup)

    file_group_path = file_match.evidence.path
    file_manifest = file_group_path / ".qobuz-librarian-retry.json"
    replacement_manifest = file_group_path / ".replacement-manifest"
    replacement_manifest.write_bytes(file_manifest.read_bytes())
    replacement_manifest.chmod(0o600)
    replacement_manifest.replace(file_manifest)
    assert inspect_staging_group_reference(
        file_records[0], owner).status is StagingReferenceStatus.CHANGED


def test_quarantine_restores_a_public_replacement_raced_into_private_staging(
        staging, monkeypatch):
    source = staging / "run" / "song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"sealed audio")
    replacement = source.with_name("incoming.flac")
    replacement.write_bytes(b"late replacement")
    receipt = capture_file(source)
    real_rename = staging_module._rename_noreplace
    raced = False

    def replace_before_move(source_fd, source_name, destination_fd, destination_name):
        nonlocal raced
        if not raced and source_name == source.name and destination_name == source.name:
            replacement.replace(source)
            raced = True
        return real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(staging_module, "_rename_noreplace", replace_before_move)

    assert quarantine_file(receipt) is None
    assert raced is True
    assert source.read_bytes() == b"late replacement"
    retry_root = staging / cfg.BEETS_RETRY_DIR
    assert not list(retry_root.rglob(source.name))


