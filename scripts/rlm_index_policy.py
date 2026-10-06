"""Deterministic compatibility policy for validated RLM indexes."""
from __future__ import annotations

from typing import Any


def receipt_is_current(receipt: dict[str, Any], *, now: float, max_age_seconds: float,
                       source_fingerprint: str, index_identity: str, tool_version: str) -> bool:
    checked = receipt.get("validated_at")
    return (
        receipt.get("schema_version") == 1
        and isinstance(checked, (int, float)) and not isinstance(checked, bool)
        and 0 <= now - checked <= max_age_seconds
        and bool(source_fingerprint) and receipt.get("source_fingerprint") == source_fingerprint
        and bool(index_identity) and receipt.get("index_identity") == index_identity
        and bool(tool_version) and receipt.get("tool_version") == tool_version
    )


def validation_succeeded(exit_code: int, before: str, after: str, index_status: str) -> bool:
    # A successful process alone never proves freshness or a consistent source.
    return exit_code == 0 and bool(before) and before == after and index_status.casefold() == "fresh"
