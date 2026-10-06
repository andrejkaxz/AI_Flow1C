"""Filesystem adapter for section drafts and approvals.

The decision rules live in :mod:`flow1c_sections_policy`; this module only
locates files, persists state atomically and appends non-sensitive audit data.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from flow1c_sections_policy import (SectionPolicyError, approve_content, catalog_summary,
        content_sha256, evaluate_checklist, next_version, parse_catalog_markdown,
        resolve_section_names, transition_state, validate_catalog, validate_section_content)
except ModuleNotFoundError:
    from scripts.flow1c_sections_policy import (SectionPolicyError, approve_content, catalog_summary,
        content_sha256, evaluate_checklist, next_version, parse_catalog_markdown,
        resolve_section_names, transition_state, validate_catalog, validate_section_content)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalog(catalog_path: Path) -> dict[str, Any]:
    catalog = parse_catalog_markdown(catalog_path.read_text(encoding="utf-8"))
    validate_catalog(catalog)
    return catalog


def storage_dir(root: Path, *, request_id: str | None, work_reference: str | None, section_id: str) -> Path:
    if not section_id or "/" in section_id or "\\" in section_id or section_id in {".", ".."}:
        raise ValueError("Invalid section_id path")
    if work_reference:
        base = root / "work-items" / _safe_slug(work_reference) / "specification" / "sections"
    else:
        if not request_id:
            raise ValueError("request_id or work_reference is required")
        base = root / "drafts" / _safe_slug(request_id) / "sections"
    return base / section_id


def _safe_slug(value: str) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."} or any(char in text for char in "\x00\r\n"):
        raise ValueError("Invalid request/reference")
    text = "".join(char if char.isalnum() or char in "-_." else "-" for char in text)
    text = text.strip(" .-")
    if not text:
        raise ValueError("Invalid request/reference")
    return text[:120]


def paths_for(root: Path, *, request_id: str | None, work_reference: str | None, section_id: str) -> tuple[Path, Path]:
    directory = storage_dir(root, request_id=request_id, work_reference=work_reference, section_id=section_id)
    return directory / "content.md", directory / "state.json"


def load_state(root: Path, *, request_id: str | None, work_reference: str | None, section_id: str, catalog_section: dict[str, Any] | None = None) -> tuple[Path, Path, str, dict[str, Any]]:
    content_path, state_path = paths_for(root, request_id=request_id, work_reference=work_reference, section_id=section_id)
    content = content_path.read_text(encoding="utf-8") if content_path.is_file() else ""
    state = _read_json(state_path)
    if state is None:
        # Compatibility with work-items created before section state existed.
        state = {"schema_version": 1, "section_id": section_id, "version": 0,
                 "content_sha256": content_sha256(content), "state": "COLLECTING_INPUT",
                 "checklist": [], "sources": []}
        if catalog_section and content:
            state["checklist"] = [{"item_id": item["item_id"], "status": "open"} for item in catalog_section["required_items"]]
    return content_path, state_path, content, state


def append_audit(root: Path, event: dict[str, Any]) -> None:
    audit = root / ".workspace" / "audit" / "section-events.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": utc_now(), **event}, ensure_ascii=False) + "\n"
    with audit.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)


def save_section(root: Path, *, section: dict[str, Any], content: str, checklist: Any, sources: list[Any] | None,
                 request_id: str | None, work_reference: str | None) -> dict[str, Any]:
    content_path, state_path, previous_content, previous = load_state(root, request_id=request_id, work_reference=work_reference, section_id=section["section_id"], catalog_section=section)
    normalized = evaluate_checklist(section, checklist)
    new_state = transition_state(str(previous.get("state", "COLLECTING_INPUT")), "save")
    normalized_content = str(content).replace("\r\n", "\n").rstrip() + "\n"
    validate_section_content(section["section_id"], normalized_content)
    state = {
        "schema_version": 1, "section_id": section["section_id"], "version": next_version(previous),
        "content_sha256": content_sha256(normalized_content), "state": new_state,
        "checklist": normalized, "sources": list(sources or []),
        "approved_content_sha256": None, "approved_at": None, "approved_by": None, "approval_statement": None,
    }
    _atomic_write(content_path, normalized_content)
    _write_json(state_path, state)
    append_audit(root, {"action": "section-save", "section_id": section["section_id"], "version": state["version"], "content_sha256": state["content_sha256"], "request_id": request_id, "work_reference": work_reference})
    return {"state": "DRAFT_READY", "section_id": section["section_id"], "version": state["version"], "content_sha256": state["content_sha256"], "content_path": str(content_path), "state_path": str(state_path), "checklist": normalized}


def approve_section(root: Path, *, section: dict[str, Any], approved_by: str, approval_statement: str,
                    request_id: str | None, work_reference: str | None) -> dict[str, Any]:
    content_path, state_path, content, previous = load_state(root, request_id=request_id, work_reference=work_reference, section_id=section["section_id"], catalog_section=section)
    validate_section_content(section["section_id"], content)
    state = approve_content(previous, content, approved_by=approved_by, approval_statement=approval_statement, approved_at=utc_now())
    _write_json(state_path, state)
    append_audit(root, {"action": "section-approve", "section_id": section["section_id"], "version": state.get("version"), "content_sha256": state["content_sha256"], "approved_content_sha256": state["approved_content_sha256"], "request_id": request_id, "work_reference": work_reference})
    return {"state": "APPROVED", "section_id": section["section_id"], "version": state.get("version"), "content_sha256": state["content_sha256"], "approved_content_sha256": state["approved_content_sha256"], "approval_statement": state["approval_statement"], "state_path": str(state_path)}
