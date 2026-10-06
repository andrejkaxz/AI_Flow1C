"""Extract explicit fields from one bounded 1C XML metadata object.

Only fields visible in the selected export are reported. Platform generated
fields and virtual tables are intentionally outside this contract.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.parsers import expat
from typing import Any


OBJECT_KINDS = {
    "Catalog": "Справочник",
    "Document": "Документ",
    "InformationRegister": "РегистрСведений",
    "AccumulationRegister": "РегистрНакопления",
    "AccountingRegister": "РегистрБухгалтерии",
    "CalculationRegister": "РегистрРасчета",
    "ChartOfAccounts": "ПланСчетов",
    "Enum": "Перечисление",
    "Constant": "Константа",
    "BusinessProcess": "БизнесПроцесс",
    "Task": "Задача",
}
FIELD_KINDS = {"Attribute", "Dimension", "Resource", "AccountingFlag", "ExtDimensionAccountingFlag"}


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((part for part in element if _tag(part) == name), None)


def _name(element: ET.Element) -> str:
    properties = _child(element, "Properties")
    field = _child(properties, "Name") if properties is not None else None
    return (field.text or "").strip() if field is not None else ""


def _fields(element: ET.Element) -> list[dict[str, Any]]:
    children = _child(element, "ChildObjects")
    if children is None:
        return []
    result = []
    for item in children:
        kind = _tag(item)
        name = _name(item)
        if kind in FIELD_KINDS and name:
            properties = _child(item, "Properties")
            type_node = _child(properties, "Type") if properties is not None else None
            type_tokens = []
            if type_node is not None:
                type_tokens = list(dict.fromkeys(
                    (part.text or "").strip() for part in type_node.iter()
                    if not list(part) and (part.text or "").strip()
                ))[:16]
            result.append({"name": name, "kind": kind, "type_tokens": type_tokens})
    return result


def parse_metadata_xml(data: bytes) -> dict[str, Any]:
    """Return exact user-defined fields or reject an unsupported XML layout."""
    scanner = expat.ParserCreate()
    def reject_doctype(*_arguments: Any) -> None:
        raise ValueError("metadata XML must not contain a DOCTYPE declaration")
    scanner.StartDoctypeDeclHandler = reject_doctype
    try:
        scanner.Parse(data, True)
        root = ET.fromstring(data)
    except (ET.ParseError, expat.ExpatError) as exc:
        raise ValueError(f"invalid metadata XML: {exc}") from exc
    if _tag(root) != "MetaDataObject":
        raise ValueError("metadata XML must have a MetaDataObject root")
    objects = [item for item in root if _tag(item) in OBJECT_KINDS]
    if len(objects) != 1:
        raise ValueError("metadata XML must contain exactly one supported object")
    obj = objects[0]
    kind = _tag(obj)
    name = _name(obj)
    if not name:
        raise ValueError("metadata XML object has no Properties/Name")
    prefix = OBJECT_KINDS[kind]
    fields = _fields(obj)
    tables = [{"name": f"{prefix}.{name}", "kind": kind, "fields": fields,
               "field_coverage": "EXPLICIT_XML_ONLY"}]
    children = _child(obj, "ChildObjects")
    if children is not None:
        for item in children:
            if _tag(item) != "TabularSection":
                continue
            section_name = _name(item)
            if section_name:
                tables.append({"name": f"{prefix}.{name}.{section_name}",
                               "kind": "TabularSection", "fields": _fields(item),
                               "field_coverage": "EXPLICIT_XML_ONLY"})
    return {"object_name": name, "object_kind": kind, "tables": tables,
            "completeness": "PARTIAL",
            "limitations": ["Стандартные поля, виртуальные таблицы и состояние базы не входят в XML-проверку."]}
