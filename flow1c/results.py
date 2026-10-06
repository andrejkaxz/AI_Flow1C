"""Data returned by operations; presentation belongs to the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationResult:
    value: Any
    exit_code: int = 0
