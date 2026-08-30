from __future__ import annotations


PERMISSION_MODES = frozenset({"request", "risk", "full"})
_LEGACY_MODES = {"ask": "risk", "always": "request"}


def normalize_permission_mode(value: object, *, default: str = "risk") -> str:
    mode = str(value or "").strip().lower()
    mode = _LEGACY_MODES.get(mode, mode)
    return mode if mode in PERMISSION_MODES else default
