"""Section approvals, DOCX plans and interview orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import storage as storage
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import flow1c_docx as docx
from scripts import flow1c_interview_cli as interview_cli
from scripts import flow1c_interview_policy as interview_policy
from scripts import flow1c_sections as section_store
from scripts import flow1c_sections_policy as sections_policy


def _sections_catalog(*, product_root: Path) -> dict[str, Any]:
    return section_store.load_catalog(
        product_root / "standards" / "functional-specification-sections.md"
    )


def _sections_root(work_reference: str | None, *, product_root: Path) -> Path:
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}) or {}
    configured = (
        runtime.project_root(local, product_root=product_root)
        if local.get("documentation_path")
        else None
    )
    if configured is not None and configured.exists():
        return configured
    return product_root if work_reference else product_root / ".workspace"


def _section_args(args: argparse.Namespace) -> tuple[str | None, str | None]:
    request_id = str(getattr(args, "request_id", "") or "").strip() or None
    work_reference = str(getattr(args, "work_reference", "") or "").strip() or None
    if not request_id and (not work_reference):
        raise WorkflowError(
            "Specify request_id for an independent draft or work_reference for a work-item section."
        )
    return (request_id, work_reference)


def _resolve_section_source(raw: str, *, product_root: Path) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise docx.DocxError(
            "DOCX_SOURCE_REQUIRED",
            "Источник DOCX не указан явно.",
            next_action="Укажите приложенный или явно выбранный путь к .docx.",
        )
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}) or {}
    roots = [
        product_root.resolve(),
        runtime.project_root(local, product_root=product_root).resolve(),
    ]
    roots.extend(
        (
            Path(item).expanduser().resolve()
            for item in local.get("allowed_external_paths", [])
            if str(item).strip()
        )
    )
    candidate = Path(value).expanduser()
    candidate = (
        (Path.cwd() / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    )
    if not any((storage.path_is_within(candidate, root) for root in roots)):
        raise docx.DocxError(
            "DOCX_PATH_FORBIDDEN",
            f"Путь DOCX находится вне разрешенных корней: {candidate}",
            next_action="Скопируйте файл в request/work-item или настройте разрешенный внешний корень.",
        )
    if candidate.is_symlink():
        raise docx.DocxError(
            "DOCX_PATH_FORBIDDEN",
            "Симлинки для источника DOCX запрещены.",
            next_action="Укажите обычный файл внутри разрешенного корня.",
        )
    return candidate


def _section_records(args: argparse.Namespace, *, product_root: Path) -> list[dict[str, Any]]:
    requested = list(getattr(args, "section_id", []) or [])
    if not requested:
        requested = list(getattr(args, "sections", []) or [])
    if isinstance(requested, str):
        requested = [requested]
    if not requested:
        raise sections_policy.SectionPolicyError(
            "SECTION_UNKNOWN",
            "Не указан section_id.",
            next_action="Вызовите section-catalog и укажите каноническое имя.",
        )
    return sections_policy.resolve_section_names(
        [str(item) for item in requested], _sections_catalog(product_root=product_root)
    )


def _require_section_tool(args: argparse.Namespace, tool_name: str, *, product_root: Path) -> None:
    gate_id = str(getattr(args, "gate_id", "") or "").strip()
    if not gate_id:
        return
    gate_state.require_gate_tool(
        gate_state.load_gate(gate_id, product_root=product_root),
        tool_name,
        product_root=product_root,
    )


def section_catalog(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_section", product_root=product_root)
    catalog = _sections_catalog(product_root=product_root)
    _value = {
        "schema_version": 1,
        "state": "READY",
        "sections": sections_policy.catalog_summary(catalog),
        "contracts": {section["section_id"]: section for section in catalog["sections"]},
    }
    return OperationResult(_value, 0)


def section_save(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_section", product_root=product_root)
    _sections_catalog(product_root=product_root)
    records = _section_records(args, product_root=product_root)
    if len(records) != 1:
        raise sections_policy.SectionPolicyError(
            "SECTION_AMBIGUOUS",
            "section-save сохраняет одну секцию за операцию.",
            next_action="Вызовите section-save отдельно для каждого section_id.",
        )
    request_id, work_reference = _section_args(args)
    content = (
        sys.stdin.read()
        if getattr(args, "content_stdin", False)
        else str(getattr(args, "content", "") or "")
    )
    if not content.strip():
        raise WorkflowError("Section content must be non-empty")
    checklist = getattr(args, "checklist", []) or []
    if isinstance(checklist, str):
        checklist = json.loads(checklist)
    sources = getattr(args, "sources", []) or []
    result = section_store.save_section(
        _sections_root(work_reference, product_root=product_root),
        section=records[0],
        content=content,
        checklist=checklist,
        sources=sources,
        request_id=request_id,
        work_reference=work_reference,
    )
    _value = result
    return OperationResult(_value, 0)


def section_approve(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_section", product_root=product_root)
    records = _section_records(args, product_root=product_root)
    if len(records) != 1:
        raise sections_policy.SectionPolicyError(
            "SECTION_AMBIGUOUS", "section-approve фиксирует одну секцию за операцию."
        )
    request_id, work_reference = _section_args(args)
    result = section_store.approve_section(
        _sections_root(work_reference, product_root=product_root),
        section=records[0],
        approved_by=str(getattr(args, "approved_by", "user") or "user"),
        approval_statement=str(getattr(args, "approval_statement", "") or ""),
        request_id=request_id,
        work_reference=work_reference,
    )
    _value = result
    return OperationResult(_value, 0)


def _docx_inspection(
    args: argparse.Namespace, *, product_root: Path
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    source = _resolve_section_source(
        getattr(args, "source", "") or getattr(args, "path", ""), product_root=product_root
    )
    sections = _section_records(args, product_root=product_root)
    inspection = docx.inspect_docx(source, sections)
    return (source, sections, inspection)


def docx_inspect(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_docx", product_root=product_root)
    _, _, inspection = _docx_inspection(args, product_root=product_root)
    _value = inspection
    return OperationResult(_value, 0)


def _load_section_current(
    root: Path, section: dict[str, Any], request_id: str | None, work_reference: str | None
) -> tuple[Path, str, dict[str, Any]]:
    content_path, state_path, content, state = section_store.load_state(
        root,
        request_id=request_id,
        work_reference=work_reference,
        section_id=section["section_id"],
        catalog_section=section,
    )
    return (state_path, content, state)


def _plan_directory(root: Path) -> Path:
    return (
        root / "docx-plans"
        if root.name.casefold() == ".workspace"
        else root / ".workspace" / "docx-plans"
    )


def _plan_path(raw: str, root: Path) -> Path:
    plan_dir = _plan_directory(root).resolve()
    if raw:
        path = Path(raw).expanduser().resolve()
        if not storage.path_is_within(path, plan_dir):
            raise docx.DocxError(
                "DOCX_PATH_FORBIDDEN", f"План находится вне управляемого каталога: {path}"
            )
        return path
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / f"{uuid.uuid4()}.json"


def _write_plan_json(path: Path, value: dict[str, Any]) -> None:
    storage.write_json(path, value)


def docx_write_plan(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_docx", product_root=product_root)
    source, sections, inspection = _docx_inspection(args, product_root=product_root)
    request_id, work_reference = _section_args(args)
    root = _sections_root(work_reference, product_root=product_root)
    modes: dict[str, str] = {}
    raw_modes = getattr(args, "modes", "") or ""
    if raw_modes:
        modes = json.loads(raw_modes) if isinstance(raw_modes, str) else dict(raw_modes)
    default_mode = str(getattr(args, "mode", "") or "").strip()
    by_id = {item["section_id"]: item for item in inspection["sections"]}
    operations = []
    for section in sections:
        item = by_id[section["section_id"]]
        status = item["status"]
        if status in {"NOT_FOUND", "AMBIGUOUS"}:
            code = (
                "TARGET_SECTION_NOT_FOUND" if status == "NOT_FOUND" else "TARGET_SECTION_AMBIGUOUS"
            )
            raise docx.DocxError(
                code,
                f"Целевой раздел {section['display_name']} имеет статус {status}.",
                next_action="Уточните место раздела и повторите инспекцию.",
            )
        mode = str(
            modes.get(section["section_id"], default_mode)
            or ("replace" if status in {"FOUND_EMPTY", "FOUND_PLACEHOLDER"} else "")
        ).strip()
        if status == "FOUND_CONTENT" and mode not in {"replace", "append"}:
            raise docx.DocxError(
                "REPLACE_MODE_REQUIRED",
                f"Раздел {section['display_name']} уже содержит текст.",
                next_action="Покажите существующее содержимое и явно выберите replace или append.",
            )
        state_path, content, state = _load_section_current(
            root, section, request_id, work_reference
        )
        sections_policy.validate_section_content(section["section_id"], content)
        if state.get("state") != "APPROVED":
            raise sections_policy.SectionPolicyError(
                "DRAFT_NOT_APPROVED", f"Раздел {section['section_id']} не согласован.", state=state
            )
        current_hash = hashlib.sha256(content.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        if current_hash != state.get("content_sha256") or current_hash != state.get(
            "approved_content_sha256"
        ):
            raise sections_policy.SectionPolicyError(
                "APPROVAL_STALE",
                f"Согласование {section['section_id']} устарело.",
                next_action="Сохраните новую версию и согласуйте ее заново.",
                state=state,
            )
        operations.append(
            {"section_id": section["section_id"], "mode": mode, "content_sha256": current_hash}
        )
    output = str(getattr(args, "output", "") or "").strip()
    output_path = (
        Path(output).expanduser().resolve()
        if output
        else source.with_name(f"{source.stem}-flow1c-filled.docx")
    )
    if not (
        storage.path_is_within(output_path, source.parent)
        or storage.path_is_within(output_path, root.resolve())
    ):
        raise docx.DocxError(
            "DOCX_PATH_FORBIDDEN",
            f"Выходной DOCX вне разрешенного каталога: {output_path}",
            next_action="Выберите результат рядом с источником или внутри request/work-item.",
        )
    plan = {
        "schema_version": 1,
        "state": "READY",
        "source": str(source),
        "source_sha256": inspection["source_sha256"],
        "output": str(output_path),
        "request_id": request_id,
        "work_reference": work_reference,
        "sections": operations,
        "write_command_required": True,
    }
    plan_path = _plan_path(str(getattr(args, "plan", "") or ""), root)
    _write_plan_json(plan_path, plan)
    section_store.append_audit(
        root,
        {
            "action": "docx-write-plan",
            "plan": str(plan_path),
            "source": str(source),
            "source_sha256": plan["source_sha256"],
            "section_ids": [item["section_id"] for item in sections],
        },
    )
    _value = {
        "state": "READY",
        "plan": str(plan_path),
        "source": str(source),
        "source_sha256": plan["source_sha256"],
        "output": str(output_path),
        "section_ids": [item["section_id"] for item in sections],
        "write_command_required": True,
    }
    return OperationResult(_value, 0)


def docx_write(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _require_section_tool(args, "flow1c_docx", product_root=product_root)
    raw_plan = str(getattr(args, "plan", "") or "").strip()
    if not raw_plan:
        raise docx.DocxError(
            "WRITE_COMMAND_REQUIRED",
            "Для docx-write требуется подтвержденный plan.",
            next_action="Сначала вызовите docx-write-plan.",
        )
    plan_path = Path(raw_plan).expanduser().resolve()
    plan = storage.read_json(plan_path)
    if not isinstance(plan, dict) or plan.get("state") not in {"READY", "WRITTEN"}:
        raise docx.DocxError(
            "DRAFT_NOT_READY",
            "План записи отсутствует или уже недействителен.",
            next_action="Создайте новый docx-write-plan.",
        )
    root = _sections_root(plan.get("work_reference"), product_root=product_root)
    if not storage.path_is_within(plan_path, _plan_directory(root).resolve()):
        raise docx.DocxError(
            "DOCX_PATH_FORBIDDEN",
            f"План находится вне управляемого каталога: {plan_path}",
            next_action="Используйте plan, созданный командой docx-write-plan.",
        )
    if plan.get("state") == "WRITTEN" and isinstance(plan.get("result"), dict):
        existing_result = plan["result"]
        existing_output = Path(
            str(existing_result.get("output") or plan.get("output") or "")
        ).expanduser()
        expected_output_hash = str(existing_result.get("output_sha256") or "")
        if (
            existing_output.is_file()
            and expected_output_hash
            and (docx.sha256_file(existing_output) == expected_output_hash)
        ):
            _value = {**existing_result, "state": "NO_CHANGE"}
            return OperationResult(_value, 0)
        raise docx.DocxError(
            "DOCX_WRITE_FAILED",
            "Проверенный результат из plan отсутствует.",
            next_action="Создайте новый plan; исходный DOCX не изменен.",
            preserved_state=plan,
        )
    current_source = _resolve_section_source(
        str(plan.get("source") or ""), product_root=product_root
    )
    if not current_source.is_file() or docx.sha256_file(current_source) != plan.get(
        "source_sha256"
    ):
        raise docx.DocxError(
            "DOCX_WRITE_FAILED",
            "Исходный DOCX изменился или исчез после plan.",
            next_action="Повторите docx-inspect и docx-write-plan.",
            preserved_state=plan,
        )
    operations = []
    states: list[tuple[Path, dict[str, Any]]] = []
    plan_sections = plan.get("sections", [])
    if not isinstance(plan_sections, list) or not plan_sections:
        raise docx.DocxError(
            "DRAFT_NOT_READY",
            "План не содержит целевых разделов.",
            next_action="Создайте новый docx-write-plan.",
        )
    section_ids = [
        str(item.get("section_id") or "") for item in plan_sections if isinstance(item, dict)
    ]
    if len(section_ids) != len(plan_sections) or len(set(section_ids)) != len(section_ids):
        raise docx.DocxError(
            "DRAFT_NOT_READY",
            "План содержит некорректный список разделов.",
            next_action="Создайте новый docx-write-plan.",
        )
    canonical_sections = {
        item["section_id"]: item
        for item in sections_policy.resolve_section_names(
            section_ids, _sections_catalog(product_root=product_root)
        )
    }
    output_path = Path(str(plan.get("output") or "")).expanduser().resolve()
    if output_path == current_source or not (
        storage.path_is_within(output_path, current_source.parent)
        or storage.path_is_within(output_path, root.resolve())
    ):
        raise docx.DocxError(
            "DOCX_PATH_FORBIDDEN",
            f"Выходной DOCX вне разрешенного каталога: {output_path}",
            next_action="Создайте новый plan с результатом рядом с источником или внутри request/work-item.",
        )
    for item in plan_sections:
        section = canonical_sections[item["section_id"]]
        if item.get("mode") not in {"replace", "append"}:
            raise docx.DocxError(
                "REPLACE_MODE_REQUIRED",
                f"План содержит недопустимый режим для {section['section_id']}.",
                next_action="Создайте новый docx-write-plan.",
            )
        state_path, content, state = _load_section_current(
            root, section, plan.get("request_id"), plan.get("work_reference")
        )
        sections_policy.validate_section_content(section["section_id"], content)
        sections_policy.ensure_write_authorized(state, content, write_command=True)
        if (
            content.replace("\r\n", "\n").encode("utf-8")
            and item["content_sha256"]
            != hashlib.sha256(content.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        ):
            raise sections_policy.SectionPolicyError(
                "APPROVAL_STALE",
                f"Текст {section['section_id']} изменился после plan.",
                next_action="Создайте новый plan.",
                state=state,
            )
        states.append((state_path, state))
        operations.append(
            {"section": section, "mode": item["mode"], "body_index": 0, "content": content}
        )
    for state_path, state in states:
        state["state"] = "WRITE_PENDING"
        storage.write_json(state_path, state)
    try:
        result = docx.write_docx(
            current_source, output_path, operations, expected_source_sha256=plan["source_sha256"]
        )
    except (docx.DocxError, OSError) as exc:
        for state_path, state in states:
            state["state"] = "WRITE_FAILED"
            state["write_error"] = getattr(exc, "code", "DOCX_WRITE_FAILED")
            storage.write_json(state_path, state)
        section_store.append_audit(
            root,
            {
                "action": "docx-write-failed",
                "plan": str(plan_path),
                "code": getattr(exc, "code", "DOCX_WRITE_FAILED"),
            },
        )
        raise
    for state_path, state in states:
        state["state"] = "WRITTEN"
        state["written_output"] = result["output"]
        state["output_sha256"] = result["output_sha256"]
        storage.write_json(state_path, state)
    plan["state"] = "WRITTEN"
    plan["result"] = result
    storage.write_json(plan_path, plan)
    section_store.append_audit(root, {"action": "docx-write", "plan": str(plan_path), **result})
    _value = result
    return OperationResult(_value, 0)


def interview_register(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_interview", product_root=product_root)
    if gate.get("operation") != "interview-preparation" or gate.get("mode") not in {
        "draft",
        "explore",
    }:
        raise interview_policy.InterviewError(
            "INTERVIEW_GATE_REQUIRED", "Откройте interview-preparation gate в draft или explore."
        )
    if args.action == "write" and gate["mode"] != "draft":
        raise interview_policy.InterviewError(
            "INTERVIEW_DRAFT_REQUIRED", "Запись книги доступна только в draft."
        )
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    result = interview_cli.execute(
        args.action,
        args.request,
        gate_state.request_root(gate, product_root=product_root),
        gate.get("artifacts", []),
        evidence.get("changed_files", []),
    )
    if result["state"] == "WRITTEN":
        record = result["record"]
        if not any(
            (
                item.get("path") == record["path"] and item.get("sha256") == record["sha256"]
                for item in evidence["changed_files"]
            )
        ):
            evidence["changed_files"].append({"target": "draft", **record})
        storage.write_json(evidence_path, evidence)
        gate["interview_output"] = record
        gate["output"] = record["path"]
        gate_state.save_request(gate, product_root=product_root)
    _value = result
    return OperationResult(_value, 0)
