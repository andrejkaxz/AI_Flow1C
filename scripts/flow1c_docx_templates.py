"""Generic OOXML mechanics behind flow1c_docx; unchanged package parts stay byte-identical."""
from __future__ import annotations

import copy
import io
import re
import zipfile
from pathlib import Path

from lxml import etree
try:
    import flow1c_docx as core
    from flow1c_markdown import PLACEHOLDER
    from flow1c_templates_policy import TemplateError
except ModuleNotFoundError:
    from scripts import flow1c_docx as core
    from scripts.flow1c_markdown import PLACEHOLDER
    from scripts.flow1c_templates_policy import TemplateError

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
STRIDE = 1_000_000
UNSUPPORTED = {"txbxContent", "object", "ins", "del", "altChunk", "fldChar"}


def xml(raw: bytes):
    return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                                                huge_tree=False, remove_blank_text=False))


def text(node) -> str:
    return "".join(node.xpath(".//w:t/text()", namespaces=NS))


def supported(node) -> bool:
    names = {etree.QName(e).localname for e in node.iter()}
    names.update(etree.QName(e).localname for e in node.iterancestors())
    if names & UNSUPPORTED:
        return False
    tables = node.xpath(".//w:tbl", namespaces=NS)
    return not any(t.xpath(".//w:tbl|.//w:vMerge|.//w:gridSpan", namespaces=NS) for t in tables)


def extract(path: Path) -> dict:
    targets, parts = [], []
    document = core.Document(path)
    heading_levels = {i: core._paragraph_level(p) for i, p in enumerate(document.paragraphs)}
    with zipfile.ZipFile(path) as package:
        for part in package.namelist():
            if not re.fullmatch(r"word/(document|header\d+|footer\d+)\.xml", part):
                continue
            root = xml(package.read(part))
            paragraphs = root.xpath(".//w:p", namespaces=NS)
            parts.append({"part": part, "paragraphs": len(paragraphs)})
            for i, paragraph in enumerate(paragraphs):
                value = text(paragraph)
                if len(value) >= STRIDE:
                    raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "DOCX paragraph exceeds parsing budget.")
                fields = list(PLACEHOLDER.finditer(value))
                if fields:
                    for match in fields:
                        targets.append({"id": f"target-{len(targets)}", "kind": "field", "part": part,
                                        "paragraph": i, "offset": list(match.span()), "text": match.group(),
                                        "range": [i * STRIDE + match.start(), i * STRIDE + match.end()],
                                        "supported": supported(paragraph)})
                elif value.strip():
                    targets.append({"id": f"target-{len(targets)}", "kind": "paragraph", "part": part,
                                    "paragraph": i, "text": value, "range": [i * STRIDE, (i + 1) * STRIDE],
                                    "supported": supported(paragraph) and not paragraph.xpath(".//w:drawing|.//w:hyperlink", namespaces=NS)})
            for i, table in enumerate(root.xpath(".//w:tbl", namespaces=NS)):
                descendants = table.xpath(".//w:p", namespaces=NS)
                indices = [paragraphs.index(p) for p in descendants]
                targets.append({"id": f"target-{len(targets)}", "kind": "table", "part": part, "table": i,
                                "text": text(table), "range": [min(indices) * STRIDE, (max(indices) + 1) * STRIDE],
                                "supported": supported(table) and not table.xpath(".//w:tbl|.//w:vMerge|.//w:gridSpan", namespaces=NS),
                                "columns": len(table.xpath("./w:tr[1]/w:tc", namespaces=NS))})
            if part == "word/document.xml":
                body = root.find(core.qn("w:body"))
                direct = body.findall(core.qn("w:p"))
                headings = [(list(body).index(p), p, heading_levels[i]) for i, p in enumerate(direct) if heading_levels.get(i)]
                for pos, (body_index, heading, level) in enumerate(headings):
                    end = next((b for b, p, l in headings[pos + 1:] if l <= level), len(body) - 1)
                    nodes = list(body)[body_index + 1:end]
                    para_indices = [paragraphs.index(p) for n in nodes for p in ([n] if n.tag == core.qn("w:p") else n.xpath(".//w:p", namespaces=NS))]
                    start_coord = min(para_indices) * STRIDE if para_indices else (paragraphs.index(heading) + 1) * STRIDE
                    end_coord = (max(para_indices) + 1) * STRIDE if para_indices else start_coord
                    targets.append({"id": f"target-{len(targets)}", "kind": "section", "part": part,
                                    "body_index": body_index, "body_end": end, "title": text(heading), "level": level,
                                    "text": "\n".join(text(n) for n in nodes), "range": [start_coord, end_coord],
                                    "supported": all(supported(n) and not n.xpath(".//w:drawing|.//w:sectPr", namespaces=NS) for n in nodes)})
                for node in root.xpath(".//w:bookmarkStart|.//w:sdtPr/w:tag", namespaces=NS):
                    parts.append({"anchor": etree.QName(node).localname,
                                  "name": node.get(core.qn("w:name")) or node.get(core.qn("w:val")),
                                  "write_support": "use the containing unambiguous paragraph/section target"})
        package_parts = len(package.namelist())
        relationships = []
        for part in package.namelist():
            if part.endswith(".rels"):
                for relationship in xml(package.read(part)):
                    relationships.append({"part": part, "id": relationship.get("Id"),
                                          "target": relationship.get("Target"), "type": relationship.get("Type"),
                                          "external": relationship.get("TargetMode") == "External"})
    return {"targets": targets, "blocks": parts, "resources": [],
            "metadata": {"styles": [s.name for s in document.styles], "sections": len(document.sections),
                         "section_settings": [{name: int(value) if value is not None else None for name in
                            ("page_width", "page_height", "top_margin", "bottom_margin", "left_margin", "right_margin", "header_distance", "footer_distance")
                            for value in [getattr(section, name)]} for section in document.sections],
                         "relationships": relationships, "numbering_preserved": True, "package_parts": package_parts}}


def replace_text(paragraph, start: int, end: int, value: str) -> None:
    nodes = paragraph.xpath(".//w:t", namespaces=NS)
    offset, inserted = 0, False
    for node in nodes:
        old = node.text or ""
        left, right = offset, offset + len(old)
        if right > start and left < end:
            prefix = old[:max(0, start - left)]
            suffix = old[max(0, end - left):]
            node.text = prefix + (value if not inserted else "") + suffix
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            inserted = True
        offset = right
    if not inserted:
        raise TemplateError("TEMPLATE_ANCHOR_AMBIGUOUS", "Text target no longer exists.")


def write_bytes(path: Path, extraction: dict, operations: list[dict]) -> bytes:
    targets = {t["id"]: t for t in extraction["targets"]}
    document = core.Document(path)
    changed = {}
    with zipfile.ZipFile(path) as package:
        for part in sorted({targets[op["target_id"]]["part"] for op in operations}):
            root = xml(package.read(part))
            paragraphs = root.xpath(".//w:p", namespaces=NS)
            tables = root.xpath(".//w:tbl", namespaces=NS)
            body = root.find(core.qn("w:body"))
            original_body = list(body) if body is not None else []
            selected = [op for op in operations if targets[op["target_id"]]["part"] == part]
            for op in sorted(selected, key=lambda o: targets[o["target_id"]]["range"][0], reverse=True):
                t = targets[op["target_id"]]
                if not t["supported"]:
                    raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Unsupported OOXML write target.")
                if op["kind"] == "text" and t["kind"] in {"field", "paragraph"}:
                    paragraph = paragraphs[t["paragraph"]]
                    start, end = t.get("offset", [0, len(text(paragraph))])
                    value = str(op["value"])
                    if "\n" in value or "\r" in value or "\t" in value:
                        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Use a section operation for multiline DOCX content.")
                    replace_text(paragraph, start, end, value)
                elif op["kind"] == "table" and t["kind"] == "table":
                    rows = op["value"]
                    if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) != t["columns"] for row in rows):
                        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Rows must match the fixed table columns.")
                    table = tables[t["table"]]
                    original = table.findall(core.qn("w:tr"))
                    if len(original) < 2:
                        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "An explicit data-row exemplar is required.")
                    exemplar = copy.deepcopy(original[1])
                    for row in original[1:]:
                        table.remove(row)
                    for values in rows:
                        row = copy.deepcopy(exemplar)
                        for cell, value in zip(row.findall(core.qn("w:tc")), values):
                            ps = cell.findall(core.qn("w:p"))
                            p = ps[0]
                            for extra in ps[1:]:
                                cell.remove(extra)
                            core._set_paragraph_text(p, str(value), copy.deepcopy(p))
                        table.append(row)
                elif op["kind"] == "section" and t["kind"] == "section":
                    anchor = original_body[t["body_index"]]
                    # Old, already validated extractions may predate this safety check.
                    # A paragraph-level sectPr controls page layout and header/footer bindings.
                    if any(n.xpath(".//w:sectPr", namespaces=NS)
                           for n in original_body[t["body_index"] + 1:t["body_end"]]):
                        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Section replacement would remove a DOCX section break; use contained text targets instead.")
                    candidate = {"kind": "heading", "body_index": t["body_index"], "end_index": t["body_end"], "title": t["title"], "level": t["level"]}
                    nodes = list(core._markdown_nodes(str(op["value"]), document, candidate))
                    for old in original_body[t["body_index"] + 1:t["body_end"]]:
                        body.remove(old)
                    index = list(body).index(anchor) + 1
                    for node in nodes:
                        body.insert(index, node)
                        index += 1
                else:
                    raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Typed operation does not match the OOXML target.")
            if PLACEHOLDER.search(text(root)):
                raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "DOCX output contains unfilled placeholders.")
            changed[part] = etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as output:
            for info in package.infolist():
                raw = changed.get(info.filename, package.read(info.filename))
                if info.filename.endswith((".xml", ".rels")):
                    xml(raw)
                output.writestr(info, raw)
    return result.getvalue()
