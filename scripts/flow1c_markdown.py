"""Bounded CommonMark/GFM extraction and range-based, non-executing writeback."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from flow1c_templates_policy import TemplateError
except ModuleNotFoundError:
    from scripts.flow1c_templates_policy import TemplateError

MAX_MARKDOWN_BYTES = 10 * 1024 * 1024
MAX_TOKENS = 200_000
PLACEHOLDER = re.compile(r"\{\{[^{}\r\n]{1,200}\}\}|\[\s*(?:заполнить|вставить|placeholder)[^\]\r\n]{0,200}\]", re.I)


def parser():
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise TemplateError("TEMPLATE_DEPENDENCY_UNAVAILABLE", "markdown-it-py is unavailable.",
                            next_action="Use bootstrap.ps1 -Profile template-markdown after approving its dependency plan.") from exc
    return MarkdownIt("commonmark", {"maxNesting": 20, "html": True}).enable("table")


def read_source(path: Path) -> str:
    if path.stat().st_size > MAX_MARKDOWN_BYTES:
        raise TemplateError("TEMPLATE_FORMAT_UNSUPPORTED", "Markdown exceeds 10 MiB; split the template.")
    return path.read_bytes().decode("utf-8-sig")


def parse(text: str):
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise TemplateError("TEMPLATE_FORMAT_UNSUPPORTED", "Markdown exceeds 10 MiB.")
    tokens = parser().parse(text)
    if sum(1 + len(t.children or []) for t in tokens) > MAX_TOKENS:
        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Markdown exceeds 200,000 tokens; split the template.")
    return tokens


def extract(path: Path) -> dict[str, Any]:
    return extract_text(read_source(path))


def extract_text(text: str) -> dict[str, Any]:
    tokens = parse(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    targets, blocks, resources = [], [], []
    excluded = []
    headings = []
    for pos, token in enumerate(tokens):
        if token.map:
            start, end = (offsets[i] for i in token.map)
            blocks.append({"type": token.type, "lines": token.map, "range": [start, end], "markup": token.markup})
            if token.type in {"fence", "code_block", "html_block"}:
                excluded.append((start, end))
            if token.type == "heading_open":
                title = tokens[pos + 1].content
                headings.append({"id": f"section-{len(headings)}", "title": title,
                                 "level": int(token.tag[1:]), "start": start, "body_start": end})
            if token.type == "table_open":
                # Opening table map spans the complete table in markdown-it-py.
                targets.append({"id": f"table-{len(targets)}", "kind": "table", "range": [start, end],
                                "supported": True, "text": text[start:end], "part": "markdown"})
        for child in token.children or []:
            if child.type in {"link_open", "image"}:
                href = child.attrGet("src" if child.type == "image" else "href") or ""
                if href and not re.match(r"^[a-z][a-z0-9+.-]*:|^//|^#", href, re.I):
                    resources.append(href)
    for i, heading in enumerate(headings):
        end = next((h["start"] for h in headings[i + 1:] if h["level"] <= heading["level"]), len(text))
        start = heading["body_start"]
        supported = not any(start <= a < end for a, b in excluded)
        targets.append({"id": heading["id"], "kind": "section", "title": heading["title"],
                        "level": heading["level"], "range": [start, end], "supported": supported,
                        "text": text[start:end], "part": "markdown"})
    for match in PLACEHOLDER.finditer(text):
        if any(a <= match.start() < b for a, b in excluded):
            continue
        targets.append({"id": f"field-{len(targets)}", "kind": "field", "range": list(match.span()),
                        "supported": True, "text": match.group(), "part": "markdown",
                        "table_cell": any(t["kind"] == "table" and t["range"][0] <= match.start() < t["range"][1] for t in targets)})
    if not targets:
        targets.append({"id": "body-0", "kind": "section", "range": [0, len(text)],
                        "supported": not excluded, "text": text, "part": "markdown"})
    return {"targets": targets, "blocks": blocks, "resources": sorted(set(resources)),
            "metadata": {"parser": "markdown-it-py", "dialect": "CommonMark+GFM-tables", "max_nesting": 20}}


def escape(value: str, *, table: bool = False) -> str:
    value = value.replace("\\", "\\\\")
    value = re.sub(r"([`*_{}\[\]()<>#!+|~])", r"\\\1", value)
    return value.replace("\r\n", "\n").replace("\n", "<br>" if table else " ")


def write_bytes(source: Path, extraction: dict, operations: list[dict]) -> bytes:
    text = read_source(source)
    targets = {t["id"]: t for t in extraction["targets"]}
    replacements = []
    for operation in operations:
        target = targets[operation["target_id"]]
        if not target["supported"]:
            raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Unsupported Markdown write region.")
        kind = operation["kind"]
        if kind == "text" and target["kind"] == "field":
            replacement = escape(str(operation["value"]), table=target.get("table_cell", False))
        elif kind == "section" and target["kind"] == "section":
            replacement = str(operation["value"]).strip() + "\n\n"
            inserted = parse(replacement)
            if any(t.type in {"heading_open", "html_block"} for t in inserted) or any(
                    c.type == "html_inline" for t in inserted for c in t.children or []):
                raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Section replacement cannot alter headings or inject HTML.")
        elif kind == "table" and target["kind"] == "table":
            original = target["text"].splitlines()
            columns = len([t for t in parse(target["text"]) if t.type == "th_open"])
            rows = operation["value"]
            if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != columns for row in rows):
                raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Table rows must match the fixed column count.")
            replacement = "\n".join(original[:2]) + "\n"
            replacement += "".join("| " + " | ".join(escape(str(v), table=True) for v in row) + " |\n" for row in rows)
        else:
            raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Operation does not match the Markdown target.")
        replacements.append((*target["range"], replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    output_extraction = extract_text(text)
    if any(t["kind"] == "field" for t in output_extraction["targets"]):
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Output contains unfilled placeholders.")
    if set(output_extraction["resources"]) - set(extraction["resources"]):
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "New local resources require a new accepted template revision.")
    original_headings = [t.content for t in parse(read_source(source)) if t.type == "inline" and t.map and
                         any(b["type"] == "heading_open" and b["lines"] == t.map for b in extraction["blocks"])]
    output_tokens = parse(text)
    output_headings = [output_tokens[i + 1].content for i, t in enumerate(output_tokens) if t.type == "heading_open"]
    if original_headings != output_headings:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Markdown heading hierarchy changed.")
    return text.encode("utf-8")
