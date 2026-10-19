"""Addressed OOXML invariants and plan-value checks; never invoke a writer."""
from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

from scripts.flow1c_templates_verify import mismatch

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SLOT = "{urn:flow1c:result-verification}slot"


def _text(node: Any) -> str:
    return "".join(n.text or "" for n in node.iter(W + "t"))


def _compare(source: Any, actual: Any, slots: dict | None = None) -> None:
    if (source.tag, dict(source.attrib), source.text or "", source.tail or "") != (
            actual.tag, dict(actual.attrib), actual.text or "", actual.tail or ""):
        mismatch("immutable-ooxml")
    expected_children, actual_children = list(source), list(actual)
    cursor = 0
    for child in expected_children:
        if child.tag == SLOT and slots is not None:
            count, check = slots[child.get("key")]
            if cursor + count > len(actual_children):
                mismatch("missing-ooxml-target")
            check(actual_children[cursor:cursor + count])
            cursor += count
        else:
            if cursor >= len(actual_children):
                mismatch("missing-immutable-ooxml")
            _compare(child, actual_children[cursor], slots)
            cursor += 1
    if cursor != len(actual_children):
        mismatch("extra-ooxml")


def _properties(source: Any, actual: Any) -> None:
    if source is None or actual is None:
        if source is not actual:
            mismatch("ooxml-formatting")
    else:
        _compare(source, actual)


def _text_target(original: Any, actual: Any, edits: list[tuple[int, int, str]]) -> None:
    value = _text(original)
    for start, end, replacement in sorted(edits, reverse=True):
        value = value[:start] + replacement + value[end:]
    if _text(actual) != value:
        mismatch("planned-docx-value")
    before, after = copy.deepcopy(original), copy.deepcopy(actual)
    left, right = list(before.iter(W + "t")), list(after.iter(W + "t"))
    if len(left) != len(right):
        mismatch("text-run-structure")
    offset = 0
    for a, b in zip(left, right):
        end = offset + len(a.text or "")
        if any(end > start and offset < stop for start, stop, _ in edits):
            if b.get(SPACE) not in {a.get(SPACE), "preserve"}:
                mismatch("text-space-property")
            a.text = b.text = "verified-variable"
            a.attrib.pop(SPACE, None)
            b.attrib.pop(SPACE, None)
        offset = end
    _compare(before, after)


def _inline(value: str) -> list[tuple[str, bool]]:
    value = re.sub(r"\\([#*`_])", r"\1", value)
    pieces, cursor = [], 0
    for match in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", value):
        if match.start() > cursor:
            pieces.append((value[cursor:match.start()], False))
        pieces.append((match.group(1) if match.group(1) is not None else "«" + match.group(2) + "»",
                       match.group(1) is not None))
        cursor = match.end()
    if cursor < len(value):
        pieces.append((value[cursor:], False))
    return pieces


def _paragraph(actual: Any, pieces: list[tuple[str, bool]], exemplar: Any = None,
               *, style: str | None = None, attributes: dict | None = None) -> None:
    if actual.tag != W + "p" or dict(actual.attrib) != (attributes or {}):
        mismatch("paragraph-structure")
    if len(actual.findall(W + "pPr")) > 1 or (actual.find(W + "pPr") is not None and actual[0].tag != W + "pPr"):
        mismatch("paragraph-properties")
    expected_properties = copy.deepcopy(exemplar.find(W + "pPr")) if exemplar is not None else None
    if style:
        if expected_properties is None:
            expected_properties = actual.makeelement(W + "pPr")
        for numbering in expected_properties.findall(W + "numPr"):
            expected_properties.remove(numbering)
        pstyle = expected_properties.find(W + "pStyle")
        if pstyle is None:
            pstyle = expected_properties.makeelement(W + "pStyle")
            expected_properties.insert(0, pstyle)
        pstyle.set(W + "val", style)
    _properties(expected_properties, actual.find(W + "pPr"))
    runs = [n for n in actual if n.tag != W + "pPr"]
    if exemplar is None and pieces == [("", False)] and not runs:
        return  # Both empty-paragraph encodings are used by python-docx tables.
    if len(runs) != len(pieces):
        mismatch("paragraph-values")
    first_run = exemplar.find(W + "r") if exemplar is not None else None
    base = first_run.find(W + "rPr") if first_run is not None else None
    for run, (value, bold) in zip(runs, pieces):
        if run.tag != W + "r" or run.attrib:
            mismatch("paragraph-run")
        props = copy.deepcopy(base)
        if bold:
            if props is None:
                props = run.makeelement(W + "rPr")
            flag = props.find(W + "b")
            if flag is None:
                flag = props.makeelement(W + "b")
                props.append(flag)
            flag.set(W + "val", "1")
        _properties(props, run.find(W + "rPr"))
        content = [n for n in run if n.tag != W + "rPr"]
        if not value and not content:
            continue
        if (len(content) != 1 or content[0].tag != W + "t" or content[0].attrib or len(content[0])
                or (content[0].text or "") != value):
            mismatch("planned-paragraph-value")


def _table(original: Any, actual: Any, rows: list, *, fixed_header: bool) -> None:
    before_rows, after_rows = original.findall(W + "tr"), actual.findall(W + "tr")
    if len(after_rows) != len(rows) + int(fixed_header):
        mismatch("table-row-count")
    before, after = copy.deepcopy(original), copy.deepcopy(actual)
    for tree in (before, after):
        for row in tree.findall(W + "tr"):
            tree.remove(row)
    _compare(before, after)
    if fixed_header:
        _compare(before_rows[0], after_rows[0])
    for index, values in enumerate(rows):
        exemplar = before_rows[1] if fixed_header else before_rows[min(index, len(before_rows) - 1)]
        written = after_rows[index + int(fixed_header)]
        source_cells, actual_cells = exemplar.findall(W + "tc"), written.findall(W + "tc")
        if len(source_cells) != len(values) or len(actual_cells) != len(values):
            mismatch("table-column-count")
        left, right = copy.deepcopy(exemplar), copy.deepcopy(written)
        for row in (left, right):
            for cell in row.findall(W + "tc"):
                row.remove(cell)
        _compare(left, right)
        for source_cell, actual_cell, value in zip(source_cells, actual_cells, values):
            paragraphs = actual_cell.findall(W + "p")
            source_p = source_cell.find(W + "p")
            if len(paragraphs) != 1 or source_p is None:
                mismatch("table-cell-structure")
            _paragraph(paragraphs[0], [(str(value), False)], source_p, attributes=dict(source_p.attrib))
            left, right = copy.deepcopy(source_cell), copy.deepcopy(actual_cell)
            for tree in (left, right):
                for node in list(tree):
                    if node.tag == W + "p" or (not fixed_header and node.tag != W + "tcPr"):
                        tree.remove(node)
            _compare(left, right)


def _new_table(actual: Any, rows: list) -> None:
    allowed = {W + n for n in ("tbl", "tblPr", "tblStyle", "tblW", "tblLook", "tblLayout", "tblGrid",
                               "gridCol", "tr", "tc", "tcPr", "tcW", "p", "r", "t")}
    if actual.tag != W + "tbl" or any(n.tag not in allowed for n in actual.iter()):
        mismatch("new-section-table-structure")
    written = actual.findall(W + "tr")
    if len(written) != len(rows):
        mismatch("new-section-table-rows")
    for row, values in zip(written, rows):
        cells = row.findall(W + "tc")
        if len(cells) != max(map(len, rows)):
            mismatch("new-section-table-columns")
        for index, cell in enumerate(cells):
            ps = cell.findall(W + "p")
            expected = str(values[index]) if index < len(values) else ""
            if len(ps) != 1:
                mismatch("new-section-table-cell")
            _paragraph(ps[0], [(expected, False)])


def _section_blocks(value: str, target: dict, document: Any) -> list[dict]:
    # Interpret the documented line/block contract, not the writer's generated XML.
    from scripts.flow1c_docx import _style_has_numbering
    lines, blocks, counters, index = value.replace("\r\n", "\n").splitlines(), [], {}, 0
    parent = re.match(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+", target["title"])
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line.startswith("|") and index < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[index]):
            rows = []
            index -= 1
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    rows.append(cells)
                index += 1
            blocks.append({"kind": "table", "rows": rows})
            continue
        style = None
        heading = re.match(r"^\\?(#{1,6})\s+(.+)$", line)
        if heading:
            level = min(9, target["level"] + max(1, len(heading[1]) - 2))
            style, line = f"Heading {level}", heading[2]
            counters = {key: count for key, count in counters.items() if key <= level}
            counters[level] = counters.get(level, 0) + 1
            if not re.match(r"^\d+(?:\.\d+)*[.)]\s+", line) and not _style_has_numbering(document, style):
                prefix = (parent[1] + "." if parent else "") + ".".join(str(counters[k]) for k in sorted(counters))
                line = prefix + ". " + line
        elif re.match(r"^[-*+·•]\s+", line):
            style, line = "List Bullet", re.sub(r"^[-*+·•]\s+", "", line)
        elif re.match(r"^(?:\d+|[a-z])[.)]\s+", line, re.I):
            style, line = "List Number", re.sub(r"^(?:\d+|[a-z])[.)]\s+", "", line, flags=re.I)
        style_id = (document.styles[style].style_id if style in document.styles else style.replace(" ", "")) if style else None
        blocks.append({"kind": "paragraph", "pieces": _inline(line), "style": style_id, "heading": bool(heading)})
    return blocks


def verify_document(source: Path, output: Path, targets: dict, operations: list[dict]) -> None:
    from scripts import flow1c_docx as core
    from scripts.flow1c_docx_templates import xml
    from scripts.flow1c_markdown import PLACEHOLDER

    core.validate_docx_package(source)
    core.validate_docx_package(output)
    document = core.Document(source)
    by_part: dict[str, list[dict]] = {}
    for op in operations:
        by_part.setdefault(targets[op["target_id"]]["part"], []).append(op)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        if set(before.namelist()) != set(after.namelist()):
            mismatch("package-parts")
        for name in before.namelist():
            original, actual = before.read(name), after.read(name)
            if name not in by_part:
                if original != actual:
                    mismatch("immutable-package-part", target=name)
                continue
            source_root, actual_root = xml(original), xml(actual)
            if PLACEHOLDER.search(_text(actual_root)):
                mismatch("unfilled-docx-field")
            paragraphs = list(source_root.iter(W + "p"))
            tables = list(source_root.iter(W + "tbl"))
            body = source_root.find(W + "body")
            body_nodes = list(body) if body is not None else []
            slots: dict[str, tuple[int, Callable]] = {}

            def add_slot(node: Any, count: int, check: Callable) -> None:
                key = str(len(slots))
                marker = node.makeelement(SLOT, {"key": key})
                node.getparent().replace(node, marker)
                slots[key] = (count, check)

            text_edits: dict[int, list] = {}
            for op in by_part[name]:
                target = targets[op["target_id"]]
                if op["kind"] == "text":
                    if any(c in str(op["value"]) for c in "\r\n\t"):
                        mismatch("unsupported-text-value", target=target["id"])
                    p = paragraphs[target["paragraph"]]
                    start, end = target.get("offset", [0, len(_text(p))])
                    text_edits.setdefault(target["paragraph"], []).append((start, end, str(op["value"])))
                elif op["kind"] == "table":
                    if not isinstance(op["value"], list) or not op["value"] or any(
                            not isinstance(row, list) or len(row) != target["columns"] for row in op["value"]):
                        mismatch("table-plan", target=target["id"])
                    table = tables[target["table"]]
                    add_slot(table, 1, lambda nodes, table=table, rows=op["value"]: _table(table, nodes[0], rows, fixed_header=True))
                else:
                    blocks = _section_blocks(str(op["value"]), target, document)
                    candidate = {"kind": "heading", "body_index": target["body_index"], "end_index": target["body_end"]}
                    p_exemplar, table_exemplar = core._section_exemplars(document, candidate)

                    def check_section(nodes: list, blocks=blocks, p=p_exemplar, table=table_exemplar) -> None:
                        for node, block in zip(nodes, blocks):
                            if block["kind"] == "table":
                                if table is None:
                                    _new_table(node, block["rows"])
                                else:
                                    _table(table, node, block["rows"], fixed_header=False)
                            else:
                                _paragraph(node, block["pieces"], None if block["heading"] else p, style=block["style"])

                    key = str(len(slots))
                    marker = body.makeelement(SLOT, {"key": key})
                    anchor = body_nodes[target["body_index"]]
                    body.insert(list(body).index(anchor) + 1, marker)
                    for old in body_nodes[target["body_index"] + 1:target["body_end"]]:
                        body.remove(old)
                    slots[key] = (len(blocks), check_section)
            for index, edits in text_edits.items():
                p = paragraphs[index]
                add_slot(p, 1, lambda nodes, p=p, edits=edits: _text_target(p, nodes[0], edits))
            _compare(source_root, actual_root, slots)
