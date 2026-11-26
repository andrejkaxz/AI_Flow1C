"""Validate the visual format map without accessing the filesystem."""

from __future__ import annotations

import re
from typing import Any

from flow1c.errors import WorkflowError

LIMITS = {".xls": (65536, 256), ".xlsx": (1048576, 16384)}
MAX_FILE_BYTES = 24 * 1024 * 1024
RANGE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?")


class WorkbookFormatError(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _column_number(letters: str) -> int:
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - ord("A") + 1
    return column


def _shape(address: Any, suffix: str) -> tuple[int, int]:
    match = RANGE.fullmatch(address) if isinstance(address, str) else None
    if not match:
        raise WorkbookFormatError("WORKBOOK_RANGE", "Use a local A1 cell or rectangular range.")
    first_col, first_row, last_col, last_row = match.groups()
    row, column = int(first_row), _column_number(first_col)
    end_row, end_column = int(last_row or first_row), _column_number(last_col or first_col)
    max_row, max_column = LIMITS[suffix]
    if not (row <= end_row <= max_row and column <= end_column <= max_column):
        raise WorkbookFormatError("WORKBOOK_RANGE", "Range exceeds the workbook format limits or is reversed.")
    return end_row - row + 1, end_column - column + 1


def format_map(value: Any, target_suffix: str, reference_suffix: str) -> list[dict[str, Any]]:
    required = {"target_sheet", "target_range", "reference_sheet", "reference_range"}
    options = {"row_heights", "column_widths"}
    if not isinstance(value, list) or not 1 <= len(value) <= 40:
        raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", "Provide between 1 and 40 range mappings.")
    mapped_cells = 0
    result = []
    for entry in value:
        if not isinstance(entry, dict) or not required <= entry.keys() <= required | options:
            raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", "Each mapping needs two sheet names and two ranges.")
        for field in ("target_sheet", "reference_sheet"):
            name = entry[field]
            if not isinstance(name, str) or not 1 <= len(name) <= 31 or any(ord(c) < 32 for c in name):
                raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", "Provide an exact worksheet name.")
        for field in options:
            if field in entry and type(entry[field]) is not bool:
                raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", f"{field} must be a boolean.")
        rows, columns = _shape(entry["target_range"], target_suffix)
        ref_rows, ref_columns = _shape(entry["reference_range"], reference_suffix)
        if ref_rows not in (1, rows) or ref_columns not in (1, columns):
            raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", "Reference dimensions must match or be one cell wide/high.")
        mapped_cells += rows * columns
        result.append({**entry, "row_heights": entry.get("row_heights", False),
                       "column_widths": entry.get("column_widths", False)})
    if mapped_cells > 4096:
        raise WorkbookFormatError("WORKBOOK_FORMAT_MAP", "At most 4096 target cells may be formatted per book.")
    return result
