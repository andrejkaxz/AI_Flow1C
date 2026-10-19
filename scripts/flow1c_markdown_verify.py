"""Check immutable source spans and typed plan values at their actual positions."""
from __future__ import annotations

from pathlib import Path

from scripts.flow1c_templates_verify import mismatch


def verify_document(source: Path, output: Path, targets: dict, operations: list[dict]) -> None:
    from scripts.flow1c_markdown import escape, extract_text, parse, read_source

    original, written = read_source(source), read_source(output)
    structure = extract_text(written)  # Bounded, non-executing format grammar.
    if any(target["kind"] == "field" for target in structure["targets"]):
        mismatch("unfilled-markdown-field")
    if set(structure["resources"]) - set(extract_text(original)["resources"]):
        mismatch("unaccepted-markdown-resource")
    source_cursor = output_cursor = 0
    for operation in sorted(operations, key=lambda op: targets[op["target_id"]]["range"]):
        target = targets[operation["target_id"]]
        start, end = target["range"]
        fixed = original[source_cursor:start]
        if written[output_cursor:output_cursor + len(fixed)] != fixed:
            mismatch("constant-markdown", target=target["id"])
        output_cursor += len(fixed)
        if operation["kind"] == "text":
            value = escape(str(operation["value"]), table=target.get("table_cell", False))
        elif operation["kind"] == "section":
            value = str(operation["value"]).strip() + "\n\n"
            blocks = parse(value)
            if any(t.type in {"heading_open", "html_block"} for t in blocks) or any(
                    c.type == "html_inline" for t in blocks for c in t.children or []):
                mismatch("unsupported-markdown-section", target=target["id"])
        else:
            header = original[start:end].splitlines()[:2]
            columns = sum(t.type == "th_open" for t in parse(original[start:end]))
            rows = operation["value"]
            if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != columns for row in rows):
                mismatch("table-columns", target=target["id"])
            value = "\n".join(header) + "\n" + "".join(
                "| " + " | ".join(escape(str(cell), table=True) for cell in row) + " |\n" for row in rows)
        if written[output_cursor:output_cursor + len(value)] != value:
            mismatch("planned-markdown-value", target=target["id"])
        output_cursor += len(value)
        source_cursor = end
    if written[output_cursor:] != original[source_cursor:]:
        mismatch("constant-markdown-tail")
