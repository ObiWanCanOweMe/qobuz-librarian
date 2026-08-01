import os

import pytest

from qobuz_librarian.library import album_placement, release_identity
from qobuz_librarian.library.album_placement import (
    AlbumPlacementAttention,
    PlacementDisposition,
    resolve_album_placement,
)
from qobuz_librarian.library.release_identity import (
    ReleaseIdentity,
    publish_release_identity,
)


def test_new_and_same_release_keep_friendly_path(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    identity = ReleaseIdentity("qobuz", "100")

    assert resolve_album_placement(friendly, identity).destination == friendly

    friendly.mkdir(parents=True)
    publish_release_identity(friendly, identity)

    assert resolve_album_placement(friendly, identity).destination == friendly


def test_different_release_gets_complete_id_suffix(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))

    placement = resolve_album_placement(
        friendly, ReleaseIdentity("qobuz", "987654321"))

    assert placement.destination.name == "Album (2020) [qobuz-987654321]"
    assert placement.disposition is PlacementDisposition.COLLISION


def test_unmarked_occupied_friendly_path_requires_attention(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)

    with pytest.raises(AlbumPlacementAttention, match="unmarked"):
        resolve_album_placement(friendly, ReleaseIdentity("qobuz", "200"))


def test_conflicting_suffixed_path_requires_attention(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    collision = friendly.with_name("Album (2020) [qobuz-200]")
    collision.mkdir()
    publish_release_identity(collision, ReleaseIdentity("qobuz", "300"))

    with pytest.raises(AlbumPlacementAttention, match="occupied"):
        resolve_album_placement(friendly, ReleaseIdentity("qobuz", "200"))


def test_collision_truncates_stem_but_preserves_complete_suffix(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / ("A" * 80)
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    monkeypatch.setattr(album_placement, "_component_name_max", lambda _path: 48)

    result = resolve_album_placement(
        friendly, ReleaseIdentity("qobuz", "987654321"))

    assert result.destination.name.endswith(" [qobuz-987654321]")
    assert len(os.fsencode(result.destination.name)) <= 48
    assert result.destination.name.startswith("A")


def test_collision_allows_a_suffix_that_exactly_fills_the_component_limit(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / "A"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    identity = ReleaseIdentity("qobuz", "9" * 39)
    monkeypatch.setattr(album_placement, "_component_name_max", lambda _path: 48)

    result = resolve_album_placement(friendly, identity)

    assert result.destination.name == f" [qobuz-{'9' * 39}]"
    assert len(os.fsencode(result.destination.name)) == 48


def test_matching_suffixed_path_is_reused_only_with_its_manifest(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, ReleaseIdentity("qobuz", "100"))
    collision = friendly.with_name("Album (2020) [qobuz-200]")
    collision.mkdir()
    publish_release_identity(collision, ReleaseIdentity("qobuz", "200"))

    placement = resolve_album_placement(friendly, ReleaseIdentity("qobuz", "200"))

    assert placement.destination == collision
    assert placement.disposition is PlacementDisposition.COLLISION


def test_explicit_adoption_proof_reuses_an_unmarked_friendly_path(tmp_path):
    friendly = tmp_path / "Artist" / "Album (2020)"
    identity = ReleaseIdentity("qobuz", "200")
    friendly.mkdir(parents=True)

    placement = resolve_album_placement(
        friendly, identity, adopted_identity=identity)

    assert placement.destination == friendly
    assert placement.disposition is PlacementDisposition.ADOPTED


def test_same_release_refuses_a_path_replaced_after_manifest_read(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / "Album (2020)"
    displaced = tmp_path / "renamed-away"
    identity = ReleaseIdentity("qobuz", "200")
    friendly.mkdir(parents=True)
    publish_release_identity(friendly, identity)
    real_read = release_identity._read_release_identity_at

    def read_then_replace(directory_descriptor):
        value = real_read(directory_descriptor)
        friendly.rename(displaced)
        friendly.mkdir()
        return value

    monkeypatch.setattr(
        release_identity, "_read_release_identity_at", read_then_replace)

    with pytest.raises(AlbumPlacementAttention, match="valid release manifest"):
        resolve_album_placement(friendly, identity)

    assert not (friendly / release_identity.MANIFEST_NAME).exists()
    assert (displaced / release_identity.MANIFEST_NAME).exists()
