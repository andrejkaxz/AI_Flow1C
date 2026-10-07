"""Assessment and preparation of formal and independent requests."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import intake as intake_service
from flow1c import registry as registry_service
from flow1c import routing
from flow1c import setup as setup_service
from flow1c import storage as storage
from flow1c import system as system
from flow1c import templates as templates_service
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.routing_policy import legacy_proposal
from flow1c.workflow import state as gate_state
from scripts import flow1c_policy as policy
from scripts import flow1c_query_policy as query_policy
from scripts import flow1c_templates as template_library
from scripts import flow1c_templates_policy as template_policy


def build_input_message(
    found: list[dict[str, str]], required: list[dict[str, str]], conditional: list[dict[str, str]]
) -> str:
    missing = required or conditional
    label = missing[0]["label"] if missing else "описание задачи"
    return f"Для формального этапа нужно уточнить: {label}. Предоставим материал или продолжим независимым черновиком из описания в чате? Можно приложить файлы в чат или указать папку. Полный перечень сохранён в диагностике."


def begin_free_request(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    operation = str(args.operation).strip().casefold()
    if operation in {"template-management", "template-document"}:
        return templates_service.begin(args, product_root=product_root)
    query_intent = None
    if operation == "query-analysis":
        try:
            query_intent = query_policy.select_query_intent(
                str(args.summary or ""), getattr(args, "query_intent", None)
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
    explicit_git_ref = getattr(args, "git_ref", None)
    contextual_git_ref = (
        policy.extract_git_ref(str(args.summary or "")) if operation == "code-review" else None
    )
    legacy_code = getattr(args, "code", None)
    git_ref_value = explicit_git_ref or contextual_git_ref
    use_legacy_as_git_ref = bool(
        operation == "code-review"
        and (not getattr(args, "task_reference", None))
        and (not getattr(args, "project_reference", None))
        and legacy_code
        and (explicit_git_ref or str(legacy_code) == str(contextual_git_ref or ""))
    )
    if use_legacy_as_git_ref and (not git_ref_value):
        git_ref_value = legacy_code
    try:
        git_ref = policy.validate_git_ref(git_ref_value)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    reference = runtime.resolve_reference_args(
        args, ignore_legacy_code=use_legacy_as_git_ref, product_root=product_root
    )
    code = reference["work_reference"]
    mismatch = str(getattr(args, "mismatch", "") or "") or work_items.reference_mismatch(
        code, getattr(args, "requirements", []) or [], product_root=product_root
    )
    assessment = policy.assess_request(
        operation, args.mode, str(args.summary or ""), code=code, mismatch=mismatch
    )
    requested_git_refs = list(
        dict.fromkeys(
            (
                str(item).strip()
                for item in getattr(args, "git_refs", None) or []
                if str(item).strip()
            )
        )
    )
    if git_ref and git_ref not in requested_git_refs:
        requested_git_refs.insert(0, git_ref)
    target_ref = str(getattr(args, "target_ref", "") or "").strip() or "main"
    refresh_requested = bool(getattr(args, "refresh_git_refs", False))
    gate = gate_state.new_gate(
        operation,
        None,
        assessment.pop("state"),
        **assessment,
        **reference,
        reference_code=code,
        git_ref=git_ref,
        summary=str(args.summary or ""),
        git_context={
            "repository": "extension",
            "source_refs": requested_git_refs,
            "target_ref": target_ref,
            "refresh_requested": refresh_requested,
            "freshness": "REFRESH_REQUESTED" if refresh_requested else "UNKNOWN",
        },
        git_analysis_state={
            "schema_version": 1,
            "cache": {},
            "terminal": {},
            "history_search_without_evidence": 0,
            "snapshots": {},
        },
        mode_selection=getattr(args, "mode_selection", None),
        mode_history=(
            [getattr(args, "mode_selection", None)] if getattr(args, "mode_selection", None) else []
        ),
        requirements=list(getattr(args, "requirements", []) or []),
        presented_paths=list(getattr(args, "path", []) or []),
        answers=[],
        notes=[],
        artifacts=[],
        product_root=product_root,
        **getattr(args, "_route_metadata", {}),
    )
    gate["request_id"] = gate["gate_id"]
    gate["storage_kind"] = (
        "documentation"
        if storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}).get("documentation_path")
        else "workspace"
    )
    gate["available_actions"] = policy.allowed_tools_for_mode(args.mode, operation)
    gate["output"] = "result.md" if args.mode == "draft" else None
    gate["skill"] = {
        "query-analysis": "flow1c-query-analysis",
        "interview-preparation": "flow1c-interview-preparation",
    }.get(operation, "flow1c-consultation")
    if query_intent:
        gate["query_intent"] = query_intent
        gate["query_contract_version"] = 1
    if getattr(args, "requested_operation", None):
        gate["requested_operation"] = args.requested_operation
        gate["routing_reason"] = "explicit_1c_query_request"
    skill_path = product_root / ".agents" / "skills" / str(gate["skill"]) / "SKILL.md"
    gate["skill_instructions"] = (
        skill_path.read_text(encoding="utf-8")
        if skill_path.exists()
        else "Discuss the goal; use chat sources; draft without changing formal status."
    )
    gate["evidence_path"] = str(
        gate_state.request_root(gate, product_root=product_root) / "evidence.json"
    )
    storage.write_json(
        Path(gate["evidence_path"]),
        {
            "schema_version": 2,
            "gate_id": gate["gate_id"],
            "operation": operation,
            "code": None,
            "git_ref": git_ref,
            **reference,
            "artifacts": [],
            "rlm_queries": [],
            "cc_inspections": [],
            "source_reads": [],
            "git_reads": [],
            "git_analysis": [],
            "snapshots": [],
            "changed_files": [],
            "query_schemas": [],
            "query_checks": [],
        },
    )
    gate_state.save_request(gate, product_root=product_root)
    _value = gate
    return OperationResult(_value, 0 if gate["state"] == "READY" else 1)


def agent_begin(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    inputs = {field: getattr(args, field, None) for field in (
        "operation", "summary", "mode", "task_reference", "project_reference", "git_ref", "code", "g_number",
    )}
    decision, metadata = routing.begin_route(getattr(args, "route_proposal", None), inputs,
                                            product_root=product_root)
    structured = getattr(args, "route_proposal", None) is not None
    if structured and decision["status"] != "VALID":
        return OperationResult(decision, 1 if decision["status"] == "CLARIFICATION_REQUIRED" else 2)
    # Preserve legacy mode-selection reasons and the narrow query remap through their owners.
    if structured:
        args.operation = decision["operation"]
        args.mode = decision["mode"]
        args.summary = args.route_proposal["expected_outcome"]
        for field, value in decision["references"].items():
            setattr(args, field, value)
    else:
        requested = str(args.operation).strip().casefold()
        resolved = legacy_proposal(requested, str(getattr(args, "summary", "") or ""),
                                   getattr(args, "mode", None))
        args.operation = resolved["operation"]
        if args.operation != requested:
            args.requested_operation = requested
    args._route_metadata = {**metadata, "route_decision": decision}
    return _begin_checked_request(args, product_root=product_root)


def _begin_checked_request(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    stages = runtime.load_stages(product_root=product_root)
    operation = str(args.operation).strip().casefold()
    if getattr(args, "requested_operation", None) and not getattr(args, "query_intent", None):
        args.query_intent = query_policy.infer_query_request_intent(str(args.summary or ""))
    try:
        mode_selection = policy.select_request_mode(
            operation, str(getattr(args, "summary", "") or ""), getattr(args, "mode", None)
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    mode_selection["selected_at"] = storage.utc_now()
    mode = str(mode_selection["mode"])
    args.mode = mode
    args.mode_selection = mode_selection
    if mode != "formal":
        return begin_free_request(args, product_root=product_root)
    stage = stages["operations"].get(operation)
    if operation in {"consultation", "query-analysis", "workflow-review", "interview-preparation"}:
        raise WorkflowError("Use a free mode for consultation, query analysis, and workflow review")
    if not isinstance(stage, dict):
        payload = gate_state.new_gate(
            operation or "unknown",
            None,
            "NEEDS_CONFIRMATION",
            summary=str(getattr(args, "summary", "") or ""),
            presented_paths=list(getattr(args, "path", []) or []),
            user_message="Намерение неоднозначно. Задайте пользователю один вопрос, различающий возможные операции.",
            product_root=product_root,
            **getattr(args, "_route_metadata", {}),
        )
        _value = payload
        return OperationResult(_value, 1)
    try:
        git_ref = policy.validate_git_ref(getattr(args, "git_ref", None))
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    reference = runtime.resolve_reference_args(args, product_root=product_root)
    if operation == "update":
        return setup_service.begin_update_gate(
            summary=str(args.summary or ""),
            project_reference=reference.get("project_reference"),
            mode_selection=mode_selection,
            route_metadata=getattr(args, "_route_metadata", {}),
            product_root=product_root,
        )
    code = reference["work_reference"]
    registry_resolution: dict[str, Any] | None = None
    if code and stage.get("code_required"):
        registry_resolution = registry_service.resolve_registry_reference(
            str(code),
            str(getattr(args, "reference_kind", "auto") or "auto"),
            product_root=product_root,
        )
        if registry_resolution["state"] == "ambiguous":
            payload = gate_state.new_gate(
                operation,
                str(code),
                "WAITING_USER",
                summary=args.summary,
                **reference,
                reference_kind=str(getattr(args, "reference_kind", "auto") or "auto"),
                registry_resolution=registry_resolution,
                mode_selection=mode_selection,
                mode_history=[mode_selection],
                clarification={
                    "reason": "registry_reference_kind",
                    "question": "Ссылка неоднозначна. Уточните, это ID требования или номер ФС; если требование назначено нескольким ФС, укажите выбранный номер ФС.",
                    "options": registry_resolution["candidates"],
                },
                user_message="Требуется выбрать однозначную реестровую ссылку.",
                product_root=product_root,
                **getattr(args, "_route_metadata", {}),
            )
            _value = payload
            return OperationResult(_value, 1)
        if registry_resolution["state"] == "resolved":
            requested_reference = str(code)
            code = str(registry_resolution["code"])
            reference = {
                **reference,
                "task_reference": code,
                "work_reference": code,
                "requested_reference": requested_reference,
            }
    assessment = policy.assess_request(
        operation,
        mode,
        str(args.summary or ""),
        code=code,
        code_required=bool(stage.get("code_required")),
        mismatch=str(getattr(args, "mismatch", "") or ""),
    )
    if assessment["state"] == "WAITING_USER":
        payload = gate_state.new_gate(
            operation,
            code,
            "WAITING_USER",
            summary=args.summary,
            **reference,
            **{k: v for k, v in assessment.items() if k != "state"},
            product_root=product_root,
            **getattr(args, "_route_metadata", {}),
        )
        _value = payload
        return OperationResult(_value, 1)
    if stage.get("code_required") and (not code):
        payload = gate_state.new_gate(
            operation,
            None,
            "BLOCKED",
            summary=str(getattr(args, "summary", "") or ""),
            presented_paths=list(getattr(args, "path", []) or []),
            **reference,
            git_ref=git_ref,
            work_item_exists=False,
            mode_selection=mode_selection,
            mode_history=[mode_selection],
            clarification={
                "reason": "missing_work_item",
                "question": "Выберите способ продолжения без рабочего элемента.",
                "options": ["provide-reference", "create-provisional", "independent-draft"],
            },
            user_message="Укажите пользовательский идентификатор, создайте provisional work-item после registry_bypass или продолжите независимым черновиком. Flow1C никогда не создаёт идентификатор автоматически.",
            conditions=[
                {
                    "id": "missing:work_reference",
                    "category": "work_reference",
                    "message": "Пользовательский project/task reference отсутствует.",
                    "source": "stage",
                    "waivable": False,
                    "blocking": True,
                }
            ],
            remaining_blockers=[
                {
                    "id": "missing:work_reference",
                    "category": "work_reference",
                    "message": "Пользовательский project/task reference отсутствует.",
                    "source": "stage",
                    "waivable": False,
                    "blocking": True,
                }
            ],
            product_root=product_root,
            **getattr(args, "_route_metadata", {}),
        )
        _value = payload
        return OperationResult(_value, 2)
    if operation == "setup":
        profile = str(getattr(args, "profile", "") or "")
        if not profile:
            payload = gate_state.new_gate(
                operation,
                None,
                "NEEDS_CONFIRMATION",
                **reference,
                summary=str(getattr(args, "summary", "") or ""),
                presented_paths=list(getattr(args, "path", []) or []),
                clarification={
                    "reason": "profile",
                    "question": "Какой уровень работы нужен? Рекомендуемый профиль для аналитика/архитектора — analysis; full выбирается только явно.",
                    "options": [
                        "analysis",
                        "conversation",
                        "project-basic",
                        "documents",
                        "implementation",
                        "full",
                    ],
                },
                user_message="Какой уровень работы нужен? Рекомендуемый профиль — analysis; full автоматически не выбирается.",
                product_root=product_root,
                **getattr(args, "_route_metadata", {}),
            )
            _value = payload
            return OperationResult(_value, 1)
        setup_skill_path = product_root / ".agents" / "skills" / str(stage["skill"]) / "SKILL.md"
        try:
            setup_state = setup_service.read_setup_state(profile, product_root=product_root)
        except WorkflowError as exc:
            payload = gate_state.new_gate(
                operation,
                None,
                "BLOCKED",
                summary=str(getattr(args, "summary", "") or ""),
                presented_paths=list(getattr(args, "path", []) or []),
                errors=[str(exc)],
                user_message=f"Настройка заблокирована: {exc}",
                product_root=product_root,
                **getattr(args, "_route_metadata", {}),
            )
            _value = payload
            return OperationResult(_value, 2)
        setup_ready = bool(setup_state.get("ready")) and setup_state.get("state") == "READY"
        setup_gate_state = (
            "READY"
            if setup_ready
            else "BLOCKED" if setup_state.get("state") == "BLOCKED" else "NEEDS_CONFIRMATION"
        )
        payload = gate_state.new_gate(
            operation,
            None,
            setup_gate_state,
            summary=str(getattr(args, "summary", "") or ""),
            presented_paths=list(getattr(args, "path", []) or []),
            skill=str(stage["skill"]),
            skill_instructions=(
                setup_skill_path.read_text(encoding="utf-8") if setup_skill_path.is_file() else ""
            ),
            setup_state=setup_state,
            requested_profile=profile,
            action_completed="setup-audit" if setup_ready else None,
            user_message=setup_service.setup_user_message(setup_state),
            product_root=product_root,
            **getattr(args, "_route_metadata", {}),
        )
        _value = payload
        return OperationResult(_value, 1)
    manifest: dict[str, Any] = {}
    item_root: Path | None = None
    if code:
        try:
            _, manifest = work_items.load_manifest(code, product_root=product_root)
            item_root = work_items.work_item_root(code, product_root=product_root)
        except WorkflowError as exc:
            payload = gate_state.new_gate(
                operation,
                code,
                "BLOCKED",
                errors=[str(exc)],
                **reference,
                git_ref=git_ref,
                work_item_exists=False,
                mode_selection=mode_selection,
                mode_history=[mode_selection],
                registry_resolution=registry_resolution,
                reference_kind=(registry_resolution or {}).get(
                    "reference_kind", getattr(args, "reference_kind", "auto")
                ),
                clarification={
                    "reason": "missing_work_item",
                    "question": "Рабочий элемент не найден. Как продолжить?",
                    "options": ["provide-reference", "create-provisional", "independent-draft"],
                },
                user_message=f"Рабочий элемент {code} не создан. "
                + (
                    "Выбранная связка найдена в реестре; запустите обычный fs-start."
                    if (registry_resolution or {}).get("state") == "resolved"
                    else "Исправьте/предоставьте реестр, подтвердите registry_bypass и создайте provisional, либо выберите независимый черновик."
                ),
                conditions=[
                    {
                        "id": "missing:registry_traceability",
                        "category": "registry_traceability",
                        "message": (
                            f"Связка для {code} пригодна, но work-item ещё не создан."
                            if (registry_resolution or {}).get("state") == "resolved"
                            else f"Рабочий элемент {code} отсутствует в пригодной области реестра."
                        ),
                        "source": "registry",
                        "waivable": bool(stage.get("provisional_allowed"))
                        and (registry_resolution or {}).get("state") != "resolved",
                        "blocking": True,
                    }
                ],
                remaining_blockers=[
                    {
                        "id": "missing:registry_traceability",
                        "category": "registry_traceability",
                        "message": (
                            f"Связка для {code} пригодна, но work-item ещё не создан."
                            if (registry_resolution or {}).get("state") == "resolved"
                            else f"Рабочий элемент {code} отсутствует в пригодной области реестра."
                        ),
                        "source": "registry",
                        "waivable": bool(stage.get("provisional_allowed"))
                        and (registry_resolution or {}).get("state") != "resolved",
                        "blocking": True,
                    }
                ],
                product_root=product_root,
                **getattr(args, "_route_metadata", {}),
            )
            _value = payload
            return OperationResult(_value, 2)
    errors: list[str] = policy.stage_errors(stage, manifest)
    traceability_mode = (
        str(manifest.get("traceability_mode") or "registry") if manifest else "registry"
    )
    if traceability_mode == "provisional" and (not stage.get("provisional_allowed")):
        errors.append("provisional traceability is not allowed for this operation")
    if traceability_mode == "provisional" and stage.get("requires_registry_traceability"):
        errors.append("validated registry traceability is required for this operation")
    found_inputs: list[dict[str, str]] = []
    required_missing: list[dict[str, str]] = []
    if item_root:
        for relative in stage.get("required_files", []):
            path = item_root / relative
            if not gate_state.meaningful_file(path):
                required_missing.append(
                    {"id": relative, "label": f"файл {relative}", "path": str(path)}
                )
            else:
                found_inputs.append(
                    {"id": relative, "label": f"файл {relative}", "path": str(path)}
                )
        for input_set in stage.get("input_sets", []):
            alternatives = list(input_set.get("alternatives", []))
            expected = (
                "input/user-brief.md"
                if traceability_mode == "provisional"
                else "input/requirements.snapshot.yaml"
            )
            acceptable = [expected] if expected in alternatives else alternatives
            selected = next(
                (
                    relative
                    for relative in acceptable
                    if gate_state.meaningful_file(item_root / relative)
                ),
                None,
            )
            if selected:
                found_inputs.append(
                    {
                        "id": str(input_set.get("id")),
                        "label": f"источник {selected}",
                        "path": str(item_root / selected),
                    }
                )
            else:
                required_missing.append(
                    {
                        "id": str(input_set.get("id")),
                        "label": "альтернативный источник требований",
                        "path": " | ".join((str(item_root / relative) for relative in acceptable)),
                    }
                )
    _, local = runtime.load_config(product_root=product_root)
    for key in stage.get("required_config", []):
        raw = str(local.get(key, "")).strip()
        configured_file = Path(raw) if raw else None
        valid = bool(configured_file and configured_file.is_file())
        if key == "functional_spec_template" and valid:
            valid = configured_file.suffix.lower() == ".docx"
        if key == "functional_spec_template" and local.get("documentation_path"):
            try:
                library = template_library.TemplateService(product_root, local)
                usable = [
                    v
                    for v in library.list()["templates"]
                    if v["document_type"] == "functional-spec" and v["ready"]
                ]
                if usable:
                    valid = True
                    raw = str(library.store.root)
            except (template_policy.TemplateError, OSError, ValueError):
                pass
        if not valid:
            required_missing.append(
                {
                    "id": key,
                    "label": f"настройка {key}",
                    "path": raw or str(product_root / runtime.LOCAL_CONFIG_FILE),
                }
            )
        else:
            found_inputs.append({"id": key, "label": f"настройка {key}", "path": raw})
    artifact_index = intake_service.load_artifact_index(code, product_root=product_root)
    present_categories = {item.get("category") for item in artifact_index["artifacts"]}
    for item in artifact_index["artifacts"]:
        if isinstance(item, dict):
            found_inputs.append(
                {
                    "id": str(item.get("category", "artifact")),
                    "label": str(
                        stages.get("artifact_categories", {})
                        .get(item.get("category"), {})
                        .get("label", item.get("name", "материал"))
                    ),
                    "path": str(
                        runtime.project_root(product_root=product_root)
                        / str(item.get("relative_path", ""))
                    ),
                }
            )
    confirmed_absent = set(artifact_index["confirmed_absent"])
    for category in stage.get("required_artifacts", []):
        if category in present_categories:
            continue
        category_config = stages.get("artifact_categories", {}).get(category, {})
        destination = intake_service.intake_destination(
            code, category, "<intake-id>", stages, product_root=product_root
        )
        required_missing.append(
            {
                "id": category,
                "label": category_config.get("label", category),
                "path": str(destination),
            }
        )
    conditional_missing: list[dict[str, str]] = []
    for category in stage.get("confirm_absence", []):
        if category in present_categories or category in confirmed_absent:
            continue
        category_config = stages.get("artifact_categories", {}).get(category, {})
        destination = intake_service.intake_destination(
            code, category, "<intake-id>", stages, product_root=product_root
        )
        conditional_missing.append(
            {
                "id": category,
                "label": category_config.get("label", category),
                "path": str(destination),
            }
        )
    diff: dict[str, Any] | None = None
    if code and (stage.get("requires_diff") or stage.get("requires_extension_branch")):
        diff, diff_error = system.extension_git_state(code, manifest, product_root=product_root)
        if diff_error:
            errors.append(diff_error)
        elif (
            stage.get("requires_extension_branch")
            and diff
            and (diff["branch"] != diff["expected_branch"])
        ):
            errors.append(
                f"extension branch is {diff['branch']}; expected {diff['expected_branch']}"
            )
        if stage.get("requires_diff") and diff is not None and (not diff["files"]):
            required_missing.append(
                {
                    "id": "extension_diff",
                    "label": "точный Git diff расширения",
                    "path": str(diff["path"]),
                }
            )
    if stage.get("requires_validated_output") and code:
        evidence_root = work_items.work_item_root(code, product_root=product_root) / "evidence"
        validated = []
        for candidate in evidence_root.glob("*.json"):
            value = storage.read_json(candidate, {})
            if isinstance(value, dict) and value.get("completion_state") == "COMPLETE":
                validated.append(candidate.name)
        if not validated:
            errors.append("no previously validated COMPLETE evidence is available for publication")
        config, local_config = runtime.load_config(product_root=product_root)
        gitea = {**config.get("gitea", {}), **local_config.get("gitea", {})}
        reviewers = gitea.get("reviewers", {}) if isinstance(gitea.get("reviewers"), dict) else {}
        if not any((isinstance(value, list) and value for value in reviewers.values())):
            errors.append("Gitea reviewer list is not configured")
    allow_incomplete = bool(getattr(args, "allow_incomplete_draft", False))
    if allow_incomplete and "extension" in stage.get("writable_targets", []):
        errors.append("an incomplete draft may not be used for extension development")
    if allow_incomplete and stage.get("requires_validated_output"):
        errors.append("an incomplete draft may not be published")
    state = policy.readiness_state(errors, required_missing, conditional_missing, allow_incomplete)
    conditions = policy.build_conditions(
        stage,
        errors=errors,
        required_missing=required_missing,
        conditional_missing=conditional_missing,
    )
    registry_choice = operation == "registry" and any(
        (item.get("id") == "requirements_workbook" for item in required_missing)
    )
    if registry_choice:
        state = "NEEDS_CONFIRMATION"
    skill_path = product_root / ".agents" / "skills" / str(stage["skill"]) / "SKILL.md"
    payload = gate_state.new_gate(
        operation,
        code,
        state,
        **reference,
        git_ref=git_ref,
        summary=str(getattr(args, "summary", "") or ""),
        presented_paths=list(getattr(args, "path", []) or []),
        mode_selection=mode_selection,
        mode_history=[mode_selection],
        registry_resolution=registry_resolution,
        reference_kind=(registry_resolution or {}).get(
            "reference_kind", getattr(args, "reference_kind", "auto")
        ),
        skill=str(stage["skill"]),
        skill_instructions=skill_path.read_text(encoding="utf-8") if skill_path.is_file() else "",
        traceability_mode=traceability_mode,
        work_item_exists=bool(item_root),
        manifest_status=manifest.get("status") if manifest else None,
        manifest_approvals=manifest.get("approvals", {}) if manifest else {},
        context_role=stage.get("context_role"),
        output=stage.get("output"),
        errors=errors,
        found_inputs=found_inputs,
        missing_required=required_missing,
        missing_conditional=conditional_missing,
        clarification=(
            {
                "reason": "registry_unavailable",
                "question": "Реестр не предоставлен. Как продолжить?",
                "options": ["provide-or-fix-registry", "create-provisional", "independent-draft"],
            }
            if registry_choice
            else None
        ),
        diff=diff,
        conditions=conditions,
        remaining_blockers=[item for item in conditions if item.get("blocking")],
        user_message=(
            "Реестр не предоставлен. Можно предоставить/исправить реестр, продолжить provisional после явного registry_bypass или создать независимый черновик."
            if registry_choice
            else (
                build_input_message(found_inputs, required_missing, conditional_missing)
                if state == "NEEDS_INPUT"
                else (
                    "Техническая блокировка: "
                    + "; ".join(errors)
                    + (
                        "\n\n"
                        + build_input_message(found_inputs, required_missing, conditional_missing)
                        if required_missing or conditional_missing
                        else ""
                    )
                    if errors
                    else (
                        "Создан непроверенный черновой gate."
                        if state == "UNVERIFIED_DRAFT"
                        else "Входные проверки пройдены."
                    )
                )
            )
        ),
        product_root=product_root,
        **getattr(args, "_route_metadata", {}),
    )
    if traceability_mode == "provisional":
        bypass = manifest.get("registry", {}).get("bypass", {})
        if bypass:
            payload.update(compliance="DEVIATED", deviations=[bypass])
        if state == "READY":
            state = "READY_WITH_DEVIATIONS"
            payload.update(state=state, remaining_blockers=[])
    payload["state"] = state
    payload["available_actions"] = policy.available_actions(
        mode="formal", operation=operation, state=state, stage=stage
    )
    if (
        state in {"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT", "NEEDS_INPUT", "BLOCKED"}
        and code
    ):
        basis = manifest.get("requirement_basis", {})
        evidence = {
            "schema_version": 1,
            "operation": operation,
            "code": code,
            "project_reference": reference.get("project_reference"),
            "task_reference": reference.get("task_reference"),
            "work_reference": code,
            "requested_reference": reference.get("requested_reference"),
            "reference_resolution": (registry_resolution or {}).get("reference_kind"),
            "context_sha256": "",
            "artifacts": artifact_index["artifacts"],
            "diff": None,
            "diff_inventory": diff,
            "rlm_queries": [],
            "source_reads": [],
            "bsl_ls": None,
            "changed_files": [],
            "created_at": storage.utc_now(),
        }
        if basis.get("sha256"):
            evidence.update(
                traceability_mode=traceability_mode,
                requirement_basis={
                    "type": basis.get("type", "registry_snapshot"),
                    "sha256": basis["sha256"],
                },
                registry_snapshot_sha256=manifest.get("registry", {}).get("snapshot_sha256"),
                revalidation_required=traceability_mode == "provisional",
            )
        if payload.get("deviations"):
            evidence.update(
                deviations=payload.get("deviations", []),
                revalidation_required=traceability_mode == "provisional",
            )
        evidence["gate_id"] = payload["gate_id"]
        evidence_path = (
            work_items.work_item_root(code, product_root=product_root)
            / "evidence"
            / f"{operation}-{payload['gate_id']}.json"
        )
        storage.write_json(evidence_path, evidence)
        payload["evidence_path"] = str(evidence_path)
        gate_state.save_gate(payload, product_root=product_root)
    _value = payload
    return OperationResult(_value, 0 if state == "READY" else 2 if state == "BLOCKED" else 1)
