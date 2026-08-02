"""Defensive normalization for persisted Web review candidates."""

_TRUTHY_FLAGS = frozenset({"1", "true", "yes", "on"})


def candidate_payload(candidate) -> dict:
    """Return a plain payload mapping, never a persisted malformed value."""
    if type(candidate) is not dict:
        return {}
    payload = candidate.get("payload")
    return payload if type(payload) is dict else {}


def is_non_actionable(candidate) -> bool:
    """Whether a review row is informational and can never authorize work."""
    if type(candidate) is not dict:
        return False
    if candidate.get("kind") == "identity_attention":
        return True
    flag = candidate_payload(candidate).get("non_actionable")
    if flag is True:
        return True
    if type(flag) in (int, float):
        return flag != 0
    return isinstance(flag, str) and flag.strip().lower() in _TRUTHY_FLAGS


def safe_album_dir_relative(value) -> str:
    """Return an exact safe POSIX-relative album path, or an empty marker."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return ""
    parts = value.split("/")
    if value.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return ""
    return value


def candidate_album_dir_relative(candidate) -> str:
    """Stable exact album-folder identity, including legacy title fallback."""
    payload = candidate_payload(candidate)
    relative = safe_album_dir_relative(payload.get("_album_dir_rel"))
    if relative:
        return relative
    title = candidate.get("title") if type(candidate) is dict else ""
    return safe_album_dir_relative(title)


def normalize_candidate(candidate) -> dict | None:
    """Copy one persisted candidate into the safe in-memory review shape."""
    if type(candidate) is not dict:
        return None
    normalized = dict(candidate)
    normalized["payload"] = dict(candidate_payload(candidate))
    if is_non_actionable(normalized):
        normalized["selected"] = False
        relative = candidate_album_dir_relative(normalized)
        if relative:
            normalized["payload"]["_album_dir_rel"] = relative
    return normalized
