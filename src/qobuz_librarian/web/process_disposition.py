"""One authoritative interpretation of album-processing results for Web flows."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessDisposition:
    mutated_library: bool
    verified_success: bool
    publish_derived_state: bool
    retire_backup: bool
    consume_candidate: bool
    attention: str | None


_BENIGN_RESULT_KINDS = frozenset({
    "already_complete",
    "skipped_already_higher_quality",
    "skipped_has_extras",
    "upgrade_only_no_op",
    "dry_run",
    "user_skipped",
    "lossy_only",
    "no_tracks",
    "cancelled",
})

_REFRESHABLE_BENIGN_RESULT_KINDS = _BENIGN_RESULT_KINDS - {
    "cancelled",
    "dry_run",
}


def _exact_nonnegative_int(value) -> bool:
    return type(value) is int and value >= 0


def _optional_result_fields_are_well_typed(value: dict) -> bool:
    return all(
        key not in value or _exact_nonnegative_int(value[key])
        for key in ("n_ok", "n_fail", "n_lossy")
    )


def _has_no_unverified_flag(value: dict) -> bool:
    return all(
        key not in value or value[key] is False
        for key in ("upgrade_unverified", "repair_unverified")
    )


def classify_process_result(result: dict | None) -> ProcessDisposition:
    """Classify every result before a Web caller publishes success effects."""
    value = result if isinstance(result, dict) else {}
    result_kind = value.get("result")
    mutated_library = value.get("imported") is True

    if result_kind == "identity_attention":
        return ProcessDisposition(
            mutated_library=mutated_library,
            verified_success=False,
            publish_derived_state=False,
            retire_backup=False,
            consume_candidate=False,
            attention="identity",
        )

    n_ok = value.get("n_ok")
    n_fail = value.get("n_fail")
    process_success = (
        result_kind in {"downloaded", "partial"}
        and mutated_library
        and type(n_ok) is int
        and n_ok > 0
        and _exact_nonnegative_int(n_fail)
        and (
            (result_kind == "downloaded" and n_fail == 0)
            or (result_kind == "partial" and n_fail > 0)
        )
        and _has_no_unverified_flag(value)
    )
    repair_success = (
        result_kind is None
        and "backup" in value
        and mutated_library
        and type(n_ok) is int
        and n_ok > 0
        and type(n_fail) is int
        and n_fail == 0
        and _has_no_unverified_flag(value)
    )
    verified_success = process_success or repair_success
    benign = (
        result_kind in _BENIGN_RESULT_KINDS
        and value.get("imported") in (None, False)
        and type(value.get("imported")) in (type(None), bool)
        and _optional_result_fields_are_well_typed(value)
        and _has_no_unverified_flag(value)
    )

    return ProcessDisposition(
        mutated_library=mutated_library,
        verified_success=verified_success,
        publish_derived_state=(
            verified_success
            or (
                benign
                and result_kind in _REFRESHABLE_BENIGN_RESULT_KINDS
            )
        ),
        retire_backup=verified_success,
        consume_candidate=verified_success or benign,
        attention=None,
    )
