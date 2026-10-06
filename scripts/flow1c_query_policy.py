"""Conservative, deterministic checks for a 1C query candidate.

This module deliberately does not implement the 1C query grammar. Its findings
are evidence about visible text and exact exported metadata, not compilation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


QUERY_INTENTS = ("create", "review", "optimize")
MAX_QUERY_CHARS = 250_000
IDENT = r"[A-Za-zА-Яа-яЁё_][\w]*"
QUALIFIED = rf"{IDENT}(?:\.{IDENT})*"
SOURCE_PATTERN = re.compile(rf"\b(ИЗ|FROM|СОЕДИНЕНИЕ|JOIN|ПОМЕСТИТЬ|INTO)\s+({QUALIFIED})", re.IGNORECASE)
FIELD_PATTERN = re.compile(rf"\b({IDENT})\.({IDENT})(?:\.({IDENT}))?", re.IGNORECASE)
PARAMETER_PATTERN = re.compile(rf"&({IDENT})", re.IGNORECASE)
ALIAS_PATTERN = re.compile(rf"\s+КАК\s+({IDENT})\b", re.IGNORECASE)
CLAUSE_PATTERN = re.compile(r"\b(ГДЕ|WHERE|СГРУППИРОВАТЬ|GROUP|УПОРЯДОЧИТЬ|ORDER|"
                            r"СОЕДИНЕНИЕ|JOIN|ОБЪЕДИНИТЬ|UNION|ИМЕЮЩИЕ|HAVING)\b", re.IGNORECASE)


def select_query_intent(summary: str, explicit: str | None) -> str:
    """Infer the requested query result when the caller omits an explicit intent."""
    if explicit is not None:
        if explicit not in QUERY_INTENTS:
            raise ValueError("query_intent must be create, review, or optimize")
        return explicit
    text = str(summary or "").casefold()
    if re.search(r"оптимиз|ускор|производительн|optimi[sz]|speed up", text):
        return "optimize"
    if re.search(r"состав|напиш|напис|созда|созд|постро|сформир|подготов|write|create|build", text):
        return "create"
    return "review"


def infer_query_request_intent(summary: str) -> str | None:
    """Route an unmistakable 1C query request away from generic consultation."""
    text = str(summary or "").strip().casefold()
    if not re.search(r"\bзапрос\w*\b|\bquery\b", text[:240]):
        return None
    if not re.search(r"1[сc]|скд|регистр|документ\.|\bвыбрать\b|\bselect\b", text[:240]):
        return None
    verbs = (
        ("optimize", r"(?:оптимизир\w*|ускор\w*|optimi[sz]\w*|speed\s+up)"),
        ("review", r"(?:провер\w*|проверь|проанализир\w*|review\w*)"),
        ("create", r"(?:напис\w*|напиш\w*|состав\w*|созда\w*|созд\w*|"
                   r"постро\w*|сформир\w*|подготов\w*|write|create|build)"),
    )
    for intent, verb in verbs:
        if re.match(rf"^(?:{verb})\b", text):
            return intent
    if re.match(r"^(?:нужен|нужны|требуется|хочу|дайте)\s+запрос\w*\b", text):
        return "create"
    return None


def _mask_literals(text: str) -> tuple[str, list[dict[str, str]]]:
    """Retain offsets but ignore quoted strings and comments during inspection."""
    chars = list(text)
    issues: list[dict[str, str]] = []
    index = 0
    while index < len(text):
        start = index
        if text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                index = len(text)
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            if end < 0:
                issues.append({"code": "UNCLOSED_COMMENT", "severity": "error", "message": "Незакрытый комментарий /*...*/."})
        elif text[index] in {'"', "'"}:
            quote = text[index]
            index += 1
            while index < len(text):
                if text[index] == quote:
                    index += 1
                    if index < len(text) and text[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            else:
                issues.append({"code": "UNCLOSED_STRING", "severity": "error", "message": "Незакрытый строковый литерал."})
        else:
            index += 1
            continue
        for offset in range(start, index):
            if chars[offset] != "\n":
                chars[offset] = " "
    return "".join(chars), issues


def analyze_query(text: str, schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Check only statements whose meaning can be established without 1C."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUERY_CHARS:
        raise ValueError(f"query text must contain 1..{MAX_QUERY_CHARS} characters")
    masked, diagnostics = _mask_literals(text)
    source_matches = list(SOURCE_PATTERN.finditer(masked))
    known_tables = {
        str(table.get("name", "")).casefold(): (schema, table)
        for schema in schemas for table in schema.get("tables", [])
        if isinstance(table, dict) and table.get("name")
    }
    aliases: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    sources: list[dict[str, str]] = []
    temporary: set[str] = set()
    outer_join_aliases: set[str] = set()
    join_count = 0
    source_spans: list[tuple[int, int]] = []
    for match in source_matches:
        keyword, name = match.group(1).casefold(), match.group(2)
        source_spans.append(match.span(2))
        if keyword in {"поместить", "into"}:
            temporary.add(name.casefold())
            continue
        schema, table = known_tables.get(name.casefold(), (None, None))
        status = "CONFIRMED_IN_XML" if table else "UNKNOWN"
        sources.append({"name": name, "status": status,
                        "evidence_id": str(schema.get("id", "")) if schema else ""})
        alias_match = ALIAS_PATTERN.match(masked, match.end(2))
        alias = alias_match.group(1) if alias_match else name.rsplit(".", 1)[-1]
        aliases[alias.casefold()] = (schema, table)
        if keyword in {"соединение", "join"}:
            join_count += 1
            if re.search(r"\b(?:ЛЕВОЕ|LEFT)\s+$", masked[max(0, match.start() - 32):match.start()], re.IGNORECASE):
                outer_join_aliases.add(alias)
    for source in sources:
        if source["name"].casefold() in temporary:
            source["status"] = "TEMPORARY_UNCHECKED"
    fields: list[dict[str, str]] = []
    seen_fields: set[tuple[str, str]] = set()
    for match in FIELD_PATTERN.finditer(masked):
        if any(start <= match.start() < end for start, end in source_spans):
            continue
        alias, field = match.group(1), match.group(2)
        owner = aliases.get(alias.casefold())
        if owner is None:
            continue  # A qualified metadata path or another unsupported expression.
        schema, table = owner
        if match.group(3):
            field = f"{field}.{match.group(3)}"
        key = (alias.casefold(), field.casefold())
        if key in seen_fields:
            continue
        seen_fields.add(key)
        known_fields = {str(item.get("name", "")).casefold(): item for item in (table or {}).get("fields", [])}
        field_metadata = known_fields.get(field.casefold())
        status = "CONFIRMED_IN_XML" if field_metadata else "UNKNOWN"
        fields.append({"alias": alias, "name": field, "status": status,
                       "type_tokens": list(field_metadata.get("type_tokens", [])) if field_metadata else [],
                       "evidence_id": str(schema.get("id", "")) if status == "CONFIRMED_IN_XML" and schema else ""})
        if status == "UNKNOWN":
            diagnostics.append({"code": "FIELD_UNVERIFIED", "severity": "warning",
                                "message": f"Поле {alias}.{field} не подтверждено доступным XML; оно может быть стандартным или вычисляемым."})
    for source in sources:
        if source["status"] == "UNKNOWN":
            diagnostics.append({"code": "SOURCE_UNVERIFIED", "severity": "warning",
                                "message": f"Источник {source['name']} не подтверждён доступными метаданными."})
    if not re.search(r"\b(ВЫБРАТЬ|SELECT)\b", masked, re.IGNORECASE):
        diagnostics.append({"code": "SELECT_NOT_FOUND", "severity": "warning",
                            "message": "Не найдено ключевое слово ВЫБРАТЬ; синтаксис запроса не подтверждён."})
    if not sources:
        diagnostics.append({"code": "SOURCE_NOT_FOUND", "severity": "warning",
                            "message": "Не найден источник после ИЗ/FROM; конструкция требует ручной проверки."})
    # A broad structural warning, not a claim that the join is wrong.
    for match in re.finditer(r"\b(?:СОЕДИНЕНИЕ|JOIN)\b", masked, re.IGNORECASE):
        remainder = masked[match.end():]
        boundary = CLAUSE_PATTERN.search(remainder)
        fragment = remainder[:boundary.start()] if boundary else remainder
        if not re.search(r"\b(ПО|ON)\b", fragment, re.IGNORECASE):
            diagnostics.append({"code": "JOIN_CONDITION_UNVERIFIED", "severity": "warning",
                                "message": "Для соединения не найдена проверяемая конструкция ПО/ON; проверьте кратность строк."})
    if join_count:
        diagnostics.append({"code": "JOIN_CARDINALITY_UNVERIFIED", "severity": "warning",
                            "message": "Кратность связей и возможное умножение строк не подтверждены по данным."})
    where_match = re.search(r"\b(?:ГДЕ|WHERE)\b", masked, re.IGNORECASE)
    if where_match:
        tail = masked[where_match.end():]
        boundary = re.search(r"\b(?:СГРУППИРОВАТЬ|GROUP|УПОРЯДОЧИТЬ|ORDER|ИМЕЮЩИЕ|HAVING|ОБЪЕДИНИТЬ|UNION)\b", tail, re.IGNORECASE)
        where_clause = tail[:boundary.start()] if boundary else tail
        for alias in sorted(outer_join_aliases):
            if re.search(rf"\b{re.escape(alias)}\.", where_clause, re.IGNORECASE):
                diagnostics.append({"code": "OUTER_JOIN_FILTER_RISK", "severity": "warning",
                                    "message": f"Условие ГДЕ с полем {alias} после левого соединения может исключить строки без совпадения."})
    if re.search(r"\b(?:СУММА|SUM|КОЛИЧЕСТВО|COUNT|СРЕДНЕЕ|AVG)\s*\(", masked, re.IGNORECASE):
        diagnostics.append({"code": "AGGREGATION_GRAIN_UNVERIFIED", "severity": "warning",
                            "message": "Проверьте зерно группировки и суммы после соединений; данные и кратность не проверены."})
    parameters = list(dict.fromkeys(PARAMETER_PATTERN.findall(masked)))
    errors = [item for item in diagnostics if item["severity"] == "error"]
    return {
        "schema_version": 1,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "verification_level": "STATIC",
        "check_result": "FAIL" if errors else "PARTIAL",
        "platform_executed": False,
        "performance_measured": False,
        "sources": sources,
        "fields": fields,
        "parameters": [{"name": name, "type": "UNKNOWN"} for name in parameters],
        "diagnostics": diagnostics,
        "limitations": [
            "Грамматика языка запросов 1С, вычисляемые поля, виртуальные таблицы и бизнес-семантика не проверены платформой.",
            "Наличие полей в XML не подтверждает состояние рабочей информационной базы.",
            "Выполнение, состав данных, план и скорость не измерялись.",
        ],
    }


def compare_optimization(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, str]]:
    """Flag visible changes that need a semantic explanation, never claim equivalence."""
    diagnostics = []
    old_sources = {item["name"].casefold() for item in baseline["sources"]}
    new_sources = {item["name"].casefold() for item in candidate["sources"]}
    if old_sources != new_sources:
        diagnostics.append({"code": "BASELINE_SOURCE_CHANGE", "severity": "warning",
                            "message": "Состав источников изменён; проверьте охват строк и связи с исходным запросом."})
    old_parameters = {item["name"].casefold() for item in baseline["parameters"]}
    new_parameters = {item["name"].casefold() for item in candidate["parameters"]}
    if old_parameters != new_parameters:
        diagnostics.append({"code": "BASELINE_PARAMETER_CHANGE", "severity": "warning",
                            "message": "Набор параметров изменён; проверьте все места установки значений."})
    if baseline["text_sha256"] == candidate["text_sha256"]:
        diagnostics.append({"code": "UNCHANGED_CANDIDATE", "severity": "warning",
                            "message": "Текст кандидата совпадает с исходным запросом; ускорение не подтверждено."})
    return diagnostics
