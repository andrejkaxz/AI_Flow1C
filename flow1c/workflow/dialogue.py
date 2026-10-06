"""Saved dialogue, answers and role context construction."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import intake as intake_service
from flow1c import storage as storage
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import begin as begin_service
from flow1c.workflow import state as gate_state
from scripts import flow1c_policy as policy

INDEPENDENT_DRAFT_DECISION_PATTERN = re.compile(
    "(?:независим\\w*\\s+черновик|отдельн\\w*\\s+черновик|без\\s+формальн\\w*\\s+привязк\\w*|independent\\s+draft)",
    re.IGNORECASE,
)


def build_role_context(
    item_root: Path,
    code: str,
    role: str,
    *,
    manifest: dict[str, Any],
    artifact_index: dict[str, Any],
) -> Path:
    """Build the existing context document from explicit work-item inputs."""
    traceability_mode = str(manifest.get("traceability_mode") or "registry")
    snapshot = storage.read_json(item_root / "input" / "requirements.snapshot.yaml", {})
    brief_path = item_root / "input" / "user-brief.md"
    role_files = {
        "analyst": ["analysis/questions.md", "analysis/answers.md", "analysis/decisions.md"],
        "functional-architect": ["analysis/traceability.md", "specification/functional-spec.md"],
        "technical-architect": [
            "analysis/traceability.md",
            "specification/functional-spec.md",
            "specification/technical-design.md",
        ],
        "tester": [
            "analysis/traceability.md",
            "specification/functional-spec.md",
            "testing/test-plan.md",
        ],
    }
    lines = [
        f"# Context: {code} — {role}",
        "",
        "> Generated file. Rebuild it instead of editing it manually.",
        "",
        f"Status: `{manifest.get('status')}`",
        f"Title: {manifest.get('title')}",
        f"Requirements basis: {('user brief' if traceability_mode == 'provisional' else 'registry snapshot')}",
        f"Traceability: {('PROVISIONAL / UNVERIFIED_DRAFT' if traceability_mode == 'provisional' else 'REGISTRY')}",
        "",
        "## Requirements basis",
        "",
    ]
    if traceability_mode == "provisional":
        lines.extend(
            [
                (
                    brief_path.read_text(encoding="utf-8-sig", errors="replace")
                    if brief_path.is_file()
                    else "User brief is missing."
                ),
                "",
            ]
        )
        if manifest.get("requirements"):
            lines.extend(
                [
                    "User-supplied identifiers (not registry-confirmed): "
                    + ", ".join(manifest["requirements"]),
                    "",
                ]
            )
    else:
        for requirement_id in manifest.get("requirements", []):
            requirement = snapshot.get(requirement_id, {})
            lines.extend(
                [
                    f"### {requirement_id}",
                    "",
                    str(requirement.get("text") or "No requirement text in the registry."),
                    "",
                    "Process: `"
                    + json.dumps(requirement.get("process", {}), ensure_ascii=False)
                    + "`",
                    "",
                ]
            )
    lines.extend(["## Work item excerpts", ""])
    for relative in role_files[role]:
        path = item_root / relative
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                lines.extend([f"### {relative}", "", value, ""])
    lines.extend(["## Accepted artifacts", ""])
    if artifact_index["artifacts"]:
        for artifact in artifact_index["artifacts"]:
            lines.append(
                f"- `{artifact.get('category')}`: `{artifact.get('relative_path')}` (SHA-256 `{artifact.get('sha256')}`)"
            )
    else:
        lines.append("No user artifacts have been accepted for this work item.")
    if artifact_index["confirmed_absent"]:
        lines.extend(["", "Confirmed absent: " + ", ".join(artifact_index["confirmed_absent"]), ""])
    target = item_root / "context" / f"{role}.md"
    storage.write_text(target, "\n".join(lines))
    return target


def agent_dialogue(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(args.gate_id, product_root=product_root)
    gate_state.require_gate_tool(gate, "flow1c_dialogue", product_root=product_root)
    if gate["state"] in {
        "COMPLETE",
        "COMPLETE_WITH_DEVIATIONS",
        "CONSULTATION_COMPLETE",
        "NON_COMPLIANT",
    }:
        raise WorkflowError("Start a new request for a completed operation")
    action = args.action
    pending = (gate.get("clarification") or {}).get("reason")
    if action == "answer" and pending == "missing_work_item":
        answer = str(args.answer or "").strip()
        if args.resolution != "independent-draft" or not INDEPENDENT_DRAFT_DECISION_PATTERN.search(
            answer
        ):
            raise WorkflowError("Select independent-draft and provide the user's explicit answer")
        reference = str(gate.get("work_reference") or gate.get("code") or "")
        selection = {
            "mode": "draft",
            "actor": "user",
            "reason": "explicit_independent_draft",
            "selected_at": storage.utc_now(),
        }
        gate.setdefault("answers", []).append(
            {"question": (gate.get("clarification") or {}).get("question", ""), "answer": answer}
        )
        gate.setdefault("deviations", []).append(
            {
                "type": "process",
                "actor": "user",
                "reason": "Formal binding was declined",
                "user_statement": answer,
                "scope": "gate",
                "condition_ids": [],
                "waived_conditions": [],
                "recorded_at": storage.utc_now(),
            }
        )
        gate.update(
            mode="draft",
            code=None,
            reference_code=reference or None,
            request_id=gate.get("request_id") or gate["gate_id"],
            storage_kind=(
                "documentation"
                if storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}).get(
                    "documentation_path"
                )
                else "workspace"
            ),
            output="result.md",
            state="READY_WITH_DEVIATIONS",
            compliance="DEVIATED",
            available_actions=policy.allowed_tools_for_mode(
                "draft", str(gate.get("operation", ""))
            ),
            mode_selection=selection,
            mode_history=[*gate.get("mode_history", []), selection],
            awaiting_user_input=False,
            clarification=None,
            remaining_blockers=[],
            user_message="Формальная привязка отклонена. Работа продолжается как независимый UNVERIFIED_DRAFT.",
        )
        gate["evidence_path"] = str(
            gate_state.request_root(gate, product_root=product_root) / "evidence.json"
        )
        storage.write_json(
            Path(gate["evidence_path"]),
            {
                "schema_version": 1,
                "gate_id": gate["gate_id"],
                "operation": gate["operation"],
                "code": None,
                "project_reference": gate.get("project_reference"),
                "task_reference": gate.get("task_reference"),
                "work_reference": gate.get("work_reference"),
                "artifacts": [],
                "rlm_queries": [],
                "source_reads": [],
                "changed_files": [],
                "deviations": gate["deviations"],
            },
        )
        gate_state.save_request(gate, product_root=product_root)
        _value = gate
        return OperationResult(_value, 0)
    if action == "deviate":
        deviation_type = str(getattr(args, "deviation_type", "") or "")
        scope = str(getattr(args, "scope", "") or "")
        condition_ids = getattr(args, "condition_ids", None)
        user_statement = str(getattr(args, "user_statement", "") or "").strip()
        reason = str(args.answer or "").strip()
        if (
            not deviation_type
            or not scope
            or (not isinstance(condition_ids, list))
            or (not condition_ids)
            or (not user_statement)
            or (not reason)
        ):
            raise WorkflowError(
                "deviate requires nonempty answer/reason, user_statement, deviation_type, scope and condition_ids"
            )
        stage = runtime.load_stages(product_root=product_root)["operations"].get(
            str(gate.get("operation")), {}
        )
        try:
            gate = policy.apply_deviation(
                gate,
                {
                    "reason": reason,
                    "user_statement": user_statement,
                    "deviation_type": deviation_type,
                    "scope": scope,
                    "condition_ids": condition_ids,
                    "recorded_at": storage.utc_now(),
                },
                stage,
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        reference = str(gate.get("work_reference") or gate.get("code") or "")
        has_work_item = bool(
            reference
            and (
                work_items.work_item_root(reference, product_root=product_root) / "manifest.yaml"
            ).is_file()
        )
        if deviation_type == "registry_bypass" and (not has_work_item):
            if not stage.get("provisional_allowed"):
                raise WorkflowError("This operation does not allow provisional registry bypass")
            gate.update(
                state="NEEDS_CONFIRMATION",
                work_item_exists=has_work_item,
                awaiting_user_input=False,
                user_message="registry_bypass зафиксирован. Запустите provisional-start с пользовательским идентификатором и непустым описанием.",
            )
        elif reference and gate.get("state") in {"BLOCKED", "NEEDS_INPUT", "READY_WITH_DEVIATIONS"}:
            if gate.get("evidence_path"):
                evidence_path, evidence = gate_state.evidence_for_gate(
                    gate, product_root=product_root
                )
            else:
                evidence_path = (
                    work_items.work_item_root(reference, product_root=product_root)
                    / "evidence"
                    / f"{gate['operation']}-{gate['gate_id']}.json"
                )
                evidence = {
                    "schema_version": 1,
                    "gate_id": gate["gate_id"],
                    "operation": gate["operation"],
                    "code": reference,
                    "project_reference": gate.get("project_reference"),
                    "task_reference": gate.get("task_reference"),
                    "work_reference": reference,
                    "context_sha256": "",
                    "artifacts": intake_service.load_artifact_index(
                        reference, product_root=product_root
                    )["artifacts"],
                    "diff": None,
                    "rlm_queries": [],
                    "source_reads": [],
                    "bsl_ls": None,
                    "changed_files": [],
                    "created_at": storage.utc_now(),
                }
                gate["evidence_path"] = str(evidence_path)
            evidence["deviations"] = gate["deviations"]
            evidence["conditions"] = gate.get("conditions", [])
            storage.write_json(evidence_path, evidence)
        gate_state.refresh_gate_actions(gate, product_root=product_root)
        gate_state.save_request(gate, product_root=product_root)
        _value = gate
        return OperationResult(_value, 0)
    if action == "record":
        if not str(args.answer or "").strip():
            raise WorkflowError("A nonempty user answer, assumption or open question is required")
        gate.setdefault("notes", []).append(
            {"kind": args.kind, "text": args.answer, "created_at": storage.utc_now()}
        )
    else:
        if action == "answer" and pending == "profile":
            profile = str(getattr(args, "profile", "") or args.answer or "").strip().casefold()
            if profile not in runtime.load_capabilities(product_root=product_root)["profiles"]:
                raise WorkflowError(
                    "Choose a valid setup profile; analysis is recommended and full must be explicit"
                )
            gate["state"] = "SUPERSEDED"
            gate.setdefault("answers", []).append(
                {"question": gate["clarification"]["question"], "answer": args.answer}
            )
            gate_state.save_gate(gate, product_root=product_root)
            return begin_service.agent_begin(
                argparse.Namespace(
                    operation="setup",
                    mode="formal",
                    code=None,
                    task_reference=None,
                    project_reference=None,
                    g_number=None,
                    profile=profile,
                    reference_kind="auto",
                    git_ref=None,
                    summary=gate.get("summary", ""),
                    path=gate.get("presented_paths", []),
                    requirements=[],
                    mismatch="",
                    allow_incomplete_draft=False,
                ),
                product_root=product_root,
            )
        if action == "answer" and pending == "registry_reference_kind":
            resolution = gate.get("registry_resolution", {})
            selected_kind = str(getattr(args, "reference_kind", "") or "").strip().casefold()
            selected_reference = str(
                args.code or gate.get("requested_reference") or gate.get("work_reference") or ""
            ).strip()
            if resolution.get("reason") == "multiple_specifications":
                if selected_reference not in resolution.get("candidates", []):
                    raise WorkflowError(
                        "Select one of the specification numbers returned by the gate"
                    )
                selected_kind = "specification"
            elif selected_kind not in {"requirement", "specification"}:
                raise WorkflowError(
                    "Select reference_kind=requirement or reference_kind=specification"
                )
            gate["state"] = "SUPERSEDED"
            gate.setdefault("answers", []).append(
                {"question": gate["clarification"]["question"], "answer": args.answer}
            )
            gate_state.save_gate(gate, product_root=product_root)
            return begin_service.agent_begin(
                argparse.Namespace(
                    operation=gate["operation"],
                    mode="formal",
                    code=None,
                    task_reference=selected_reference,
                    project_reference=gate.get("project_reference"),
                    reference_kind=selected_kind,
                    git_ref=gate.get("git_ref"),
                    g_number=None,
                    profile=None,
                    summary=gate.get("summary", "")
                    + "\nОтвет пользователя: "
                    + str(args.answer or ""),
                    path=gate.get("presented_paths", []),
                    requirements=gate.get("requirements", []),
                    mismatch="",
                    allow_incomplete_draft=False,
                ),
                product_root=product_root,
            )
        if action == "answer" and pending == "requirement_mismatch":
            if gate.get("mode", "formal") == "formal":
                if args.resolution not in {"independent-draft", "correct-code"}:
                    raise WorkflowError("Select independent-draft or correct-code")
                code = str(args.code or "").strip() if args.resolution == "correct-code" else None
                if args.resolution == "correct-code" and (
                    not code
                    or work_items.reference_mismatch(
                        code, gate.get("requirements", []), product_root=product_root
                    )
                ):
                    raise WorkflowError("Supply a corrected code matching the requirements")
                gate["state"] = "SUPERSEDED"
                gate.setdefault("answers", []).append(
                    {"question": gate["clarification"]["question"], "answer": args.answer}
                )
                gate_state.save_gate(gate, product_root=product_root)
                return begin_service.agent_begin(
                    argparse.Namespace(
                        operation=gate["operation"],
                        mode="draft" if code is None else "formal",
                        code=code,
                        summary=gate.get("summary", "") + "\nОтвет пользователя: " + args.answer,
                        path=gate.get("presented_paths", []),
                        requirements=gate.get("requirements", []),
                        mismatch="",
                        allow_incomplete_draft=False,
                    ),
                    product_root=product_root,
                )
            if args.resolution == "independent-draft":
                gate.update(mode="draft", code=None, reference_code=None, output="result.md")
            elif args.resolution == "correct-code":
                code = str(args.code or "").strip()
                if not code:
                    raise WorkflowError("Supply the corrected user-assigned code")
                mismatch = work_items.reference_mismatch(
                    code, gate.get("requirements", []), product_root=product_root
                )
                if mismatch:
                    raise WorkflowError(mismatch)
                gate["reference_code"] = code
            else:
                raise WorkflowError(
                    "Select independent-draft or correct-code; the registry will not be changed"
                )
        try:
            gate = policy.transition(
                gate, action, question=str(args.question or ""), answer=str(args.answer or "")
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
    if getattr(args, "mode", None):
        if gate.get("mode", "formal") == "formal" or args.mode not in {"explore", "draft"}:
            raise WorkflowError(
                "Mode changes here are only for free requests; start a new formal gate"
            )
        if gate.get("mode") != args.mode:
            selection = {
                "mode": args.mode,
                "actor": "caller",
                "reason": "explicit_dialogue_change",
                "selected_at": storage.utc_now(),
            }
            gate["mode_selection"] = selection
            gate.setdefault("mode_history", []).append(selection)
        gate["mode"] = args.mode
        gate["output"] = "result.md" if args.mode == "draft" else None
    if gate.get("mode", "formal") != "formal":
        if gate["state"] == "DRAFT_COMPLETE":
            gate["state"] = "READY"
        gate["available_actions"] = policy.allowed_tools_for_mode(
            str(gate["mode"]), str(gate.get("operation", ""))
        )
    else:
        gate_state.refresh_gate_actions(gate, product_root=product_root)
    gate_state.save_request(gate, product_root=product_root)
    _value = gate
    return OperationResult(_value, 0)


def build_context_for_reference(code: str, role: str, *, product_root: Path) -> Path:
    _, manifest = work_items.load_manifest(code, product_root=product_root)
    return build_role_context(
        work_items.work_item_root(code, product_root=product_root),
        code,
        role,
        manifest=manifest,
        artifact_index=intake_service.load_artifact_index(code, product_root=product_root),
    )


def context_build(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    _value = build_context_for_reference(args.code, args.role, product_root=product_root)
    return OperationResult(_value, 0)


def agent_context(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_context", product_root=product_root)
    code = str(gate.get("code") or "")
    role = str(gate.get("context_role") or "")
    if not code or role not in runtime.VALID_ROLES:
        raise WorkflowError("This operation does not define a role context.")
    target = build_context_for_reference(code, role, product_root=product_root).resolve()
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    isolated_target = evidence_path.parent / f"context-{gate['gate_id']}.md"
    shutil.copy2(target, isolated_target)
    target = isolated_target
    digest = storage.sha256(target)
    evidence_id = f"CTX-{digest[:12]}"
    evidence["context_sha256"] = digest
    evidence["context_path"] = str(target)
    evidence["context_evidence_id"] = evidence_id
    storage.write_json(evidence_path, evidence)
    gate["context_sha256"] = digest
    gate_state.save_gate(gate, product_root=product_root)
    _value = {
        "state": "READY",
        "evidence_id": evidence_id,
        "path": str(target),
        "sha256": digest,
        "content": target.read_text(encoding="utf-8"),
    }
    return OperationResult(_value, 0)
