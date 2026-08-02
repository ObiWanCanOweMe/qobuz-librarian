import hashlib
import os
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.library import album_placement, release_identity
from qobuz_librarian.library.album_placement import (
    AlbumPlacementAttention,
    PlacementDisposition,
    capture_legacy_adoption_receipt,
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
    receipt = capture_legacy_adoption_receipt(friendly, identity)

    placement = resolve_album_placement(
        friendly,
        identity,
        adopted_identity=identity,
        adoption_receipt=receipt,
    )

    assert placement.destination == friendly
    assert placement.disposition is PlacementDisposition.ADOPTED


def test_legacy_adoption_proof_is_rejected_after_its_scan_closes(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / "Album (2020)"
    identity = ReleaseIdentity("qobuz", "200")
    friendly.mkdir(parents=True)
    audio = friendly / "01 - Alpha.flac"
    audio.write_bytes(b"alpha-audio")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption.lock")
    scan_type = getattr(album_placement, "LegacyAdoptionScan", None)
    assert scan_type is not None, "LegacyAdoptionScan must own live adoption evidence"
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with scan_type(friendly, lease) as scan:
            proof = scan.proof(identity)
            placement = resolve_album_placement(
                friendly,
                identity,
                adopted_identity=identity,
                adoption_proof=proof,
            )
            assert placement.disposition is PlacementDisposition.ADOPTED

        with pytest.raises(AlbumPlacementAttention, match="closed|live|detached"):
            resolve_album_placement(
                friendly,
                identity,
                adopted_identity=identity,
                adoption_proof=proof,
            )
    finally:
        lease.close()


def test_live_adoption_placement_never_reopens_the_public_album_path(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import release_authority

    friendly = tmp_path / "Artist" / "Album"
    friendly.mkdir(parents=True)
    (friendly / "01.flac").write_bytes(b"audio")
    identity = ReleaseIdentity("qobuz", "200")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption-no-reopen.lock")
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with album_placement.LegacyAdoptionScan(friendly, lease) as scan:
            proof = scan.proof(identity)

            def reopened(*_args, **_kwargs):
                raise AssertionError("live adoption reopened the public album path")

            monkeypatch.setattr(
                album_placement,
                "capture_directory_path_receipt",
                reopened,
            )
            monkeypatch.setattr(
                album_placement,
                "read_release_identity_with_receipt",
                reopened,
            )
            monkeypatch.setattr(
                release_authority,
                "directory_path_receipt_matches",
                reopened,
            )

            placement = resolve_album_placement(
                friendly,
                identity,
                adopted_identity=identity,
                adoption_proof=proof,
            )
            assert placement.disposition is PlacementDisposition.ADOPTED
    finally:
        lease.close()


def test_legacy_adoption_scan_maps_clean_teardown_oserror_to_attention(
        tmp_path, monkeypatch):
    from qobuz_librarian.library.release_authority import AlbumAuthority

    friendly = tmp_path / "Artist" / "Album"
    friendly.mkdir(parents=True)
    (friendly / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption-close.lock")
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with pytest.raises(AlbumPlacementAttention, match="changed|close"):
            with album_placement.LegacyAdoptionScan(friendly, lease):
                def fail_close(*_args, **_kwargs):
                    raise OSError("descriptor close failed")

                monkeypatch.setattr(AlbumAuthority, "__exit__", fail_close)
    finally:
        lease.close()


def test_legacy_adoption_scan_reads_held_a_during_public_aba(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / "Album (2020)"
    held_a = tmp_path / "held-a"
    replacement_b = tmp_path / "replacement-b"
    displaced_b = tmp_path / "displaced-b"
    friendly.mkdir(parents=True)
    replacement_b.mkdir()
    a_bytes = b"alpha-audio-with-alpha-tags-and-isrc"
    b_bytes = b"beta-audio-with-beta-tags-and-isrc"
    (friendly / "01.flac").write_bytes(a_bytes)
    (replacement_b / "01.flac").write_bytes(b_bytes)
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption-aba.lock")
    scan_type = getattr(album_placement, "LegacyAdoptionScan", None)
    reader = getattr(album_placement, "_read_held_audio_meta", None)
    assert scan_type is not None, "LegacyAdoptionScan must own live adoption evidence"
    assert reader is not None, "held audio must have a descriptor-backed tag reader"
    observed = []

    def read_then_aba(path):
        observed.append(Path(path).read_bytes())
        friendly.rename(held_a)
        replacement_b.rename(friendly)
        friendly.rename(displaced_b)
        held_a.rename(friendly)
        return {
            "title": "Alpha",
            "isrc": "USAAA0000001",
            "discnumber": 1,
            "tracknumber": 1,
        }

    monkeypatch.setattr(album_placement, "_read_held_audio_meta", read_then_aba)
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with pytest.raises(AlbumPlacementAttention, match="changed|authority"):
            with scan_type(friendly, lease) as scan:
                scan.read_tracks()
    finally:
        lease.close()

    assert observed == [a_bytes]
    assert observed != [b_bytes]
    assert hashlib.sha256((friendly / "01.flac").read_bytes()).digest() == hashlib.sha256(
        a_bytes
    ).digest()


def test_legacy_adoption_scan_acquires_audio_in_bytewise_relative_order(
        tmp_path, monkeypatch):
    friendly = tmp_path / "Artist" / "Album"
    (friendly / "Disc 2").mkdir(parents=True)
    (friendly / "z.flac").write_bytes(b"z")
    (friendly / "Disc 2" / "01.flac").write_bytes(b"disc")
    (friendly / "A.flac").write_bytes(b"a")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption-order.lock")
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with album_placement.LegacyAdoptionScan(friendly, lease) as scan:
            assert [item.relative for item in scan.audio_receipts] == [
                "A.flac",
                "Disc 2/01.flac",
                "z.flac",
            ]
    finally:
        lease.close()


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_legacy_adoption_scan_rejects_linked_audio(
        tmp_path, monkeypatch, link_kind):
    friendly = tmp_path / "Artist" / "Album"
    friendly.mkdir(parents=True)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    linked = friendly / "01.flac"
    if link_kind == "symbolic":
        linked.symlink_to(outside)
    else:
        os.link(outside, linked)
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / f"adoption-{link_kind}.lock")
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with pytest.raises(AlbumPlacementAttention, match="link|regular|authority"):
            with album_placement.LegacyAdoptionScan(friendly, lease):
                pass
    finally:
        lease.close()


def test_legacy_adoption_scan_fails_closed_without_audio_write_exclusion(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import release_authority

    friendly = tmp_path / "Artist" / "Album"
    friendly.mkdir(parents=True)
    (friendly / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "adoption-no-lease.lock")
    monkeypatch.setattr(
        release_authority,
        "acquire_inode_write_exclusion",
        lambda _descriptor: None,
    )
    lease = run_lock.acquire()
    assert lease is not None
    try:
        with pytest.raises(AlbumPlacementAttention, match="authority|unavailable"):
            with album_placement.LegacyAdoptionScan(friendly, lease):
                pass
    finally:
        lease.close()


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
