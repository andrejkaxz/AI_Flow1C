"""Pure limits, validation and ranking for local project memory."""
from __future__ import annotations

import json
import re
from typing import Any

from flow1c.errors import WorkflowError


class ProjectMemoryError(WorkflowError):
    """A stable memory failure code shared by CLI and agent adapters."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = message.partition(":")[0]

KINDS = {"project_fact", "decision", "open_question", "source_location"}
BASES = {"user_statement", "accepted_document", "completed_result"}
SOURCE_BASIS = {"context_entry": "accepted_document", "dialogue_answer": "user_statement",
                "dialogue_note": "user_statement", "sealed_handoff": "completed_result"}
MAX_RECORDS = 200
MAX_SCAN_BYTES = 2_000_000
MAX_RESULTS = 5
MAX_RESPONSE_CHARS = 8000
ID_PATTERN = re.compile(r"[a-f0-9-]{36}\Z")


def bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ProjectMemoryError(f"MEMORY_INVALID: {name} must contain 1 to {maximum} characters")
    return value.strip()


def memory_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ProjectMemoryError("MEMORY_INVALID: invalid memory_id")
    return value


def revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ProjectMemoryError("MEMORY_INVALID: revision must be a positive integer")
    return value


def score(record: dict[str, Any], query: str) -> int:
    words = [part for part in query.casefold().split() if part]
    if not words:
        return 1
    subject = record["subject"].casefold()
    aliases = " ".join(record["aliases"]).casefold()
    body = record["text"].casefold()
    return sum(10 if word in subject else 6 if word in aliases else 1 if word in body else 0 for word in words)


def freshness(expected: str, actual: str | None) -> str:
    return "MISSING" if actual is None else "WORKING" if expected == actual else "STALE"


def response_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def response_limit(value: Any) -> int:
    if type(value) is not int or not 1500 <= value <= MAX_RESPONSE_CHARS:
        raise ProjectMemoryError("MEMORY_INVALID: max_chars must be between 1500 and 8000")
    return value
