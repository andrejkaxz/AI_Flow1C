"""Pure policy for functional-specification sections.

The module deliberately receives a catalog object instead of reading the catalog
itself. This keeps resolution, completeness, hashing and state transitions
deterministic and independent from the filesystem, DOCX and agent adapters.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from typing import Any

SECTION_STATES = (
    "COLLECTING_INPUT", "DRAFT_READY", "APPROVED", "WRITE_PENDING", "WRITTEN", "WRITE_FAILED"
)
CHECKLIST_STATUSES = ("described", "not_applicable", "open")
SECTION_ERROR_CODES = (
    "SECTION_UNKNOWN", "SECTION_AMBIGUOUS", "SECTION_REQUIRED_ITEM_OPEN", "DRAFT_NOT_READY",
    "DRAFT_NOT_APPROVED", "APPROVAL_STALE", "WRITE_COMMAND_REQUIRED", "TARGET_SECTION_NOT_EMPTY",
    "SECTION_PROVENANCE_IN_BODY",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TECHNICAL_PROVENANCE_RE = re.compile(
    r"(?i)(?<![\w/])(?:iss/\d+|(?:pr|mr)\s*[!#]?\s*\d+|"
    r"(?:issue|задач[аеуыи])\s*[#№-]?\s*\d+|"
    r"(?:ветк[а-я]*|branch)\s+[`*]*[\w./-]*\d[\w./-]*)\b"
)


class SectionPolicyError(ValueError):
    """Machine-readable deterministic policy failure."""

    def __init__(self, code: str, message: str, *, next_action: str = "", state: Any = None):
        self.code = code
        self.message = message
        self.next_action = next_action or "Correct the input and retry the same operation."
        self.state = state
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "component": "section-policy", "message": self.message,
            "recoverable": True, "next_action": self.next_action,
            "preserved_state": self.state,
        }


def parse_catalog_markdown(text: str) -> dict[str, Any]:
    """Extract the first JSON fenced block from the canonical Markdown catalog."""
    match = re.search(r"```json\s*\r?\n(.*?)\r?\n```", str(text), re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("Catalog does not contain a JSON block")
    value = json.loads(match.group(1))
    validate_catalog(value)
    return value


def validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ValueError("Unsupported functional sections catalog schema")
    sections = catalog.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Catalog sections must be a non-empty array")
    ids: set[str] = set()
    aliases: dict[str, str] = {}
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("Each catalog section must be an object")
        required = ("section_id", "display_name", "aliases", "purpose", "applicability",
                    "required_items", "sources", "structure", "value_rules", "forbidden", "word")
        missing = [key for key in required if key not in section]
        if missing:
            raise ValueError(f"Section {section.get('section_id', '<unknown>')} missing: {', '.join(missing)}")
        section_id = str(section["section_id"])
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section_id):
            raise ValueError(f"Invalid section_id: {section_id}")
        if section_id in ids:
            raise ValueError(f"Duplicate section_id: {section_id}")
        ids.add(section_id)
        items = section["required_items"]
        if not isinstance(items, list) or not items:
            raise ValueError(f"Section {section_id} must have required_items")
        item_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
                raise ValueError(f"Section {section_id} has an invalid required item")
            if item["item_id"] in item_ids:
                raise ValueError(f"Duplicate item_id in {section_id}: {item['item_id']}")
            item_ids.add(item["item_id"])
            if not isinstance(item.get("what_to_obtain"), str) or not isinstance(item.get("questions"), list):
                raise ValueError(f"Required item {item['item_id']} has an incomplete contract")
        for alias in [section_id, section["display_name"], *section["aliases"]]:
            key = normalize_section_name(alias)
            if not key:
                raise ValueError(f"Empty alias in {section_id}")
            owner = aliases.get(key)
            if owner and owner != section_id:
                raise ValueError(f"Conflicting alias {alias!r}: {owner} vs {section_id}")
            aliases[key] = section_id
    if "technical-implementation" in ids:
        technical = next(s for s in sections if s["section_id"] == "technical-implementation")
        expected = {"new-common-modules", "common-module-properties", "new-procedures-functions",
                    "changed-procedures-functions", "attribute-dimension-resource-properties"}
        actual = {item["item_id"] for item in technical["required_items"]}
        if actual != expected:
            raise ValueError("technical-implementation must contain exactly five mandatory categories")
    return catalog


def normalize_section_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"^\s*(?:раздел\s+)?\d+(?:[.):-]+\s*)+", "", text)
    text = re.sub(r"[\s\u00a0]+", " ", text)
    text = re.sub(r"[\s:;,.!?\-–—]+$", "", text)
    return text


def resolve_section_names(requested: list[str], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    validate_catalog(catalog)
    by_alias: dict[str, list[dict[str, Any]]] = {}
    for section in catalog["sections"]:
        for alias in [section["section_id"], section["display_name"], *section["aliases"]]:
            by_alias.setdefault(normalize_section_name(alias), []).append(section)
    selected: dict[str, dict[str, Any]] = {}
    for raw in requested:
        key = normalize_section_name(raw)
        matches = by_alias.get(key, [])
        if not matches:
            raise SectionPolicyError("SECTION_UNKNOWN", f"Неизвестный раздел: {raw}", next_action="Выберите раздел из section-catalog.")
        unique = {item["section_id"]: item for item in matches}
        if len(unique) != 1:
            raise SectionPolicyError("SECTION_AMBIGUOUS", f"Неоднозначный раздел: {raw}", next_action="Уточните каноническое имя раздела.")
        section = next(iter(unique.values()))
        selected[section["section_id"]] = section
    # The catalog is the template order. User wording may list sections in a
    # different order, but persistence and write plans stay deterministic.
    return [section for section in catalog["sections"] if section["section_id"] in selected]


def evaluate_checklist(section: dict[str, Any], checklist: Any) -> list[dict[str, str]]:
    """Normalize a checklist and require an explicit outcome for every item."""
    validate_catalog({"schema_version": 1, "sections": [section]})
    supplied = {str(item.get("item_id")): item for item in checklist} if isinstance(checklist, list) else {}
    allowed_ids = {item["item_id"] for item in section["required_items"]}
    unknown_ids = sorted(set(supplied) - allowed_ids)
    if unknown_ids:
        raise SectionPolicyError("SECTION_REQUIRED_ITEM_OPEN", f"Неизвестные пункты checklist: {', '.join(unknown_ids)}")
    normalized: list[dict[str, str]] = []
    for item in section["required_items"]:
        record = supplied.get(item["item_id"], {})
        status = str(record.get("status", "open"))
        if status not in CHECKLIST_STATUSES:
            raise SectionPolicyError("SECTION_REQUIRED_ITEM_OPEN", f"Недопустимый статус {status!r} для {item['item_id']}")
        entry = {"item_id": item["item_id"], "status": status}
        if record.get("reason"):
            entry["reason"] = str(record["reason"])
        normalized.append(entry)
    return normalized


def content_sha256(content: str) -> str:
    return hashlib.sha256(str(content).replace("\r\n", "\n").encode("utf-8")).hexdigest()


def validate_section_content(section_id: str, content: str) -> None:
    """Keep Git/work-item provenance outside the customer implementation text."""
    if section_id != "technical-implementation":
        return
    match = _TECHNICAL_PROVENANCE_RE.search(content)
    if match:
        raise SectionPolicyError(
            "SECTION_PROVENANCE_IN_BODY",
            f"Раздел «Техническая реализация» содержит служебную ссылку: {match.group(0)}.",
            next_action="Удалите из текста номера задач, веток и PR/MR; сохраните источники отдельно в поле sources или evidence.",
        )


def next_version(previous: dict[str, Any] | None) -> int:
    return int(previous.get("version", 0)) + 1 if isinstance(previous, dict) else 1


def transition_state(state: str, action: str) -> str:
    transitions = {
        "save": {"COLLECTING_INPUT": "DRAFT_READY", "DRAFT_READY": "DRAFT_READY", "APPROVED": "DRAFT_READY", "WRITTEN": "DRAFT_READY", "WRITE_FAILED": "DRAFT_READY"},
        "approve": {"DRAFT_READY": "APPROVED"},
        "write-plan": {"APPROVED": "WRITE_PENDING"},
        "write-success": {"WRITE_PENDING": "WRITTEN"},
        "write-failure": {"WRITE_PENDING": "WRITE_FAILED"},
    }
    if state not in transitions.get(action, {}):
        raise SectionPolicyError("DRAFT_NOT_READY" if action in {"approve", "write-plan"} else "APPROVAL_STALE", f"Недопустимый переход {state} + {action}")
    return transitions[action][state]


def approve_content(state: dict[str, Any], content: str, *, approved_by: str, approval_statement: str, approved_at: str) -> dict[str, Any]:
    current_hash = content_sha256(content)
    if state.get("state") not in {"DRAFT_READY", "WRITE_FAILED"}:
        raise SectionPolicyError("DRAFT_NOT_READY", "Согласовать можно только готовый черновик.", state=state)
    if not str(approval_statement):
        raise SectionPolicyError("DRAFT_NOT_READY", "Текст согласования не может быть пустым.", state=state)
    updated = deepcopy(state)
    updated.update(state="APPROVED", content_sha256=current_hash, approved_content_sha256=current_hash,
                   approved_at=approved_at, approved_by=str(approved_by), approval_statement=str(approval_statement))
    return updated


def ensure_write_authorized(state: dict[str, Any], current_content: str, *, write_command: bool) -> None:
    if not write_command:
        raise SectionPolicyError("WRITE_COMMAND_REQUIRED", "Для записи требуется отдельная команда пользователя.", next_action="Сначала покажите план, затем вызовите docx-write.", state=state)
    if state.get("state") not in {"APPROVED", "WRITE_FAILED", "WRITE_PENDING"}:
        raise SectionPolicyError("DRAFT_NOT_APPROVED", "Раздел не имеет актуального согласования.", state=state)
    current_hash = content_sha256(current_content)
    if state.get("approved_content_sha256") != current_hash or state.get("content_sha256") != current_hash:
        raise SectionPolicyError("APPROVAL_STALE", "Текст изменился после согласования; требуется новая версия и согласование.", state=state)


def catalog_summary(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    validate_catalog(catalog)
    return [{"section_id": s["section_id"], "display_name": s["display_name"], "aliases": list(s["aliases"])} for s in catalog["sections"]]
