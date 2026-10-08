"""Pure workflow policy; no I/O or infrastructure probes."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

MODES = ("explore", "draft", "formal")
FREE_OPERATIONS = {"interview-preparation", "template-management", "template-document", "consultation", "query-analysis", "workflow-review", "functional-spec", "functional-section", "functional-review",
                   "technical-design", "technical-implementation", "code-review", "testing"}
FREE_TOOLS = ["flow1c_intake", "flow1c_redmine_fetch", "flow1c_inspect", "flow1c_knowledge", "flow1c_git_refresh", "flow1c_git_inspect",
              "flow1c_git_snapshot", "flow1c_dialogue", "flow1c_source_query", "flow1c_complete"]
DRAFT_TOOLS = [*FREE_TOOLS, "flow1c_section", "flow1c_docx", "flow1c_source_read", "flow1c_write", "flow1c_promote"]
TERMINAL = {"COMPLETE", "COMPLETE_WITH_DEVIATIONS", "CONSULTATION_COMPLETE", "DRAFT_COMPLETE", "NON_COMPLIANT"}
CONDITION_CATEGORIES = {
    "work_reference", "registry_traceability", "required_input", "conditional_input",
    "status", "approval", "rlm_evidence", "diff_evidence", "bsl_evidence",
    "extension_branch", "path_safety", "publication", "external_confirmation",
}
HARD_NON_WAIVABLE_CATEGORIES = {
    "path_safety", "publication", "external_confirmation", "extension_branch",
}
FORMAL_DOCUMENT_OPERATIONS = {
    "functional-spec", "functional-review", "technical-design", "technical-implementation",
    "code-review", "testing",
}
REFERENCE_MAX_LENGTH = 128
CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
FORMAL_INTENT_PATTERN = re.compile(
    r"(?:формальн\w*|согласова(?:ть|ние)|утверди(?:ть|те)|опубликова(?:ть|ние)|"
    r"publish|approve|formal\s+review)", re.IGNORECASE,
)
DRAFT_INTENT_PATTERN = re.compile(
    r"(?:черновик|markdown|\.md\b|документ|отч[её]т|draft)", re.IGNORECASE,
)
SECTION_INTENT_PATTERN = re.compile(
    r"(?:раздел\w*|technical\s+implementation|техническ\w+\s+реализац\w*|"
    r"настроек\s+системы|тестов\w*\s+сценар\w*|предмет\s+разработк\w*|рол\w*\s+и\s+прав\w*)",
    re.IGNORECASE,
)
GIT_BRANCH_PATTERN = re.compile(
    r"(?:ветк(?:а|и|у|е)|branch|git[- ]?ref)\s+['\"`]?([^\s'\"`,;]+)", re.IGNORECASE,
)
GIT_COMMIT_PATTERN = re.compile(
    r"(?:коммит(?:а|е|у)?|commit)\s+['\"`]?([^\s'\"`,;]+)", re.IGNORECASE,
)


def allowed_tools_for_mode(mode: str, operation: str | None = None) -> list[str]:
    """Return a fresh, duplicate-free tool list for a non-formal mode."""
    if mode == "explore":
        tools = FREE_TOOLS
    elif mode == "draft":
        tools = DRAFT_TOOLS
    elif mode == "formal":
        tools = []
    else:
        raise ValueError(f"Unknown work mode: {mode}")
    result = list(dict.fromkeys(tools))
    if operation in {"template-management", "template-document", "functional-spec", "testing"}:
        result.insert(-1, "flow1c_template")
    if operation == "template-document" and mode == "draft":
        result.insert(-1, "flow1c_document")
    if operation == "query-analysis":
        result[-1:-1] = ["flow1c_cc_inspect", "flow1c_query_schema", "flow1c_query_check"]
    if operation == "interview-preparation":
        result.insert(-1, "flow1c_interview")
    if operation == "code-review" and mode != "formal":
        result.insert(-1, "flow1c_analyze_bsl")
    if operation in FREE_OPERATIONS:
        result.insert(-1, "flow1c_context")
    return result


def select_request_mode(operation: str, summary: str, explicit_mode: str | None) -> dict:
    """Select a mode without turning an ordinary code review into formal work."""
    if explicit_mode:
        if explicit_mode not in MODES:
            raise ValueError("Unknown work mode")
        return {"mode": explicit_mode, "actor": "caller", "reason": "explicit"}
    text = str(summary or "")
    if operation == "interview-preparation":
        return {"mode": "draft", "actor": "workflow", "reason": "interview_draft_default"}
    if operation in {"template-management", "template-document"}:
        return {"mode": "draft" if operation == "template-document" else "explore", "actor": "workflow", "reason": "template_operation"}
    if operation == "functional-section" or (operation == "functional-spec" and SECTION_INTENT_PATTERN.search(text) and not FORMAL_INTENT_PATTERN.search(text)):
        return {"mode": "draft", "actor": "workflow", "reason": "functional_section_draft"}
    if operation == "query-analysis":
        return {"mode": "explore", "actor": "workflow", "reason": "read_only_query_analysis_default"}
    if operation == "code-review":
        if FORMAL_INTENT_PATTERN.search(text):
            return {"mode": "formal", "actor": "workflow", "reason": "formal_intent"}
        if DRAFT_INTENT_PATTERN.search(text):
            return {"mode": "draft", "actor": "workflow", "reason": "document_requested"}
        return {"mode": "explore", "actor": "workflow", "reason": "read_only_review_default"}
    return {"mode": "formal", "actor": "workflow", "reason": "legacy_default"}


def extract_git_ref(summary: str) -> str | None:
    """Extract only a value explicitly introduced as a Git branch or commit."""
    text = str(summary or "")
    match = GIT_BRANCH_PATTERN.search(text) or GIT_COMMIT_PATTERN.search(text)
    return match.group(1).rstrip(".!?:)") if match else None


def validate_git_ref(value: str | None) -> str | None:
    """Validate a Git revision argument while preserving numeric branch names."""
    if value is None:
        return None
    ref = str(value).strip()
    if not ref:
        return None
    if len(ref) > 256 or CONTROL_PATTERN.search(ref) or ref.startswith("-"):
        raise ValueError("Invalid git_ref")
    return ref


def python_version_supported(version: tuple[int, int], model: dict) -> bool:
    minimum = tuple(int(part) for part in model["python"]["minimum"].split("."))
    return version >= minimum


def validate_reference(value: str | None, *, required: bool = False) -> str | None:
    """Validate an opaque user-supplied project/task reference without normalizing it."""
    if value is None:
        if required:
            raise ValueError("A project or task reference is required")
        return None
    reference = str(value).strip()
    if not reference:
        if required:
            raise ValueError("A project or task reference must not be empty")
        return None
    if len(reference) > REFERENCE_MAX_LENGTH:
        raise ValueError(f"A project or task reference must be at most {REFERENCE_MAX_LENGTH} characters")
    if CONTROL_PATTERN.search(reference):
        raise ValueError("A project or task reference must not contain control characters")
    return reference


def resolve_work_reference(task_reference: str | None, project_reference: str | None) -> dict:
    """Resolve task over project while preserving the exact value supplied by the user."""
    task = validate_reference(task_reference)
    project = validate_reference(project_reference)
    if task:
        return {"project_reference": project, "task_reference": task,
                "work_reference": task, "reference_source": "task"}
    if project:
        return {"project_reference": project, "task_reference": None,
                "work_reference": project, "reference_source": "project"}
    return {"project_reference": None, "task_reference": None,
            "work_reference": None, "reference_source": "none"}


def reference_slug(reference: str, *, suffix: str = "") -> str:
    """Build a path-safe name; callers may add a stable suffix on collision."""
    value = validate_reference(reference, required=True)
    assert value is not None
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[<>:\"/\\|?*]+", "-", value)
    value = re.sub(r"\s+", "-", value).strip(" .-")
    value = re.sub(r"-+", "-", value)
    if not value:
        value = "work-item"
    if suffix:
        value = f"{value}-{suffix}"
    return value[:120].rstrip(" .-")


def load_capability_model(value: dict) -> dict:
    """Validate the small declarative capability graph and return it unchanged."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported capabilities schema")
    capabilities = value.get("capabilities")
    profiles = value.get("profiles")
    if not isinstance(capabilities, dict) or not isinstance(profiles, dict):
        raise ValueError("Capabilities and profiles must be objects")
    for name, profile in profiles.items():
        requested = profile.get("capabilities") if isinstance(profile, dict) else None
        if not isinstance(requested, list) or any(item not in capabilities for item in requested):
            raise ValueError(f"Profile {name} references an unknown capability")
    return value


def resolve_capabilities(model: dict, profile: str) -> list[str]:
    """Resolve transitive capability dependencies in stable declaration order."""
    load_capability_model(model)
    profiles = model["profiles"]
    if profile not in profiles:
        raise ValueError(f"Unknown profile: {profile}")
    requested = profiles[profile]["capabilities"]
    resolved: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in resolved:
            return
        if name in visiting:
            raise ValueError(f"Capability dependency cycle at {name}")
        visiting.add(name)
        for dependency in model["capabilities"][name].get("requires", []):
            visit(dependency)
        visiting.remove(name)
        resolved.append(name)

    for capability in requested:
        visit(capability)
    return resolved


def operation_profile(stages: dict, operation: str) -> str:
    stage = stages.get("operations", {}).get(operation)
    if not isinstance(stage, dict):
        raise ValueError(f"Unknown operation: {operation}")
    profile = stage.get("required_profile")
    if not isinstance(profile, str) or not profile:
        raise ValueError(f"Operation {operation} has no required_profile")
    return profile


def capability_readiness(required: list[str], states: dict[str, str]) -> bool:
    return all(states.get(name) == "READY" for name in required)


def _condition_category(message: str, default: str) -> str:
    text = str(message or "").casefold()
    if "registry traceability" in text or "registry" in text and "traceability" in text:
        return "registry_traceability"
    prefixes = (
        ("status ", "status"), ("approval ", "approval"),
        ("extension branch", "extension_branch"), ("publication", "publication"),
        ("path ", "path_safety"), ("external confirmation", "external_confirmation"),
        ("no gated rlm", "rlm_evidence"), ("rlm", "rlm_evidence"),
        ("bounded extension diff", "diff_evidence"), ("diff", "diff_evidence"),
        ("bsl", "bsl_evidence"),
    )
    return next((category for prefix, category in prefixes if text.startswith(prefix) or prefix in text), default)


def make_condition(condition_id: str, category: str, message: str, *, source: str,
                   waivable: bool, blocking: bool = True) -> dict:
    if category not in CONDITION_CATEGORIES:
        raise ValueError(f"Unknown condition category: {category}")
    return {
        "id": str(condition_id), "category": category, "message": str(message),
        "source": str(source), "waivable": bool(waivable), "blocking": bool(blocking),
    }


def build_conditions(stage_policy: dict, *, errors: list[str] | None = None,
                     required_missing: list[dict] | None = None,
                     conditional_missing: list[dict] | None = None) -> list[dict]:
    """Build deterministic, auditable conditions while retaining legacy fields."""
    policy = stage_policy.get("deviation_policy", {}) if isinstance(stage_policy, dict) else {}
    waivable_categories = set(policy.get("waivable_categories", []))
    allow_documents = bool(policy.get("allow_formal_documents", False))
    conditions: list[dict] = []
    for item in required_missing or []:
        value = item if isinstance(item, dict) else {"id": str(item), "label": str(item)}
        condition_id = str(value.get("condition_id") or f"missing:{value.get('id', 'required_input')}")
        category = "diff_evidence" if str(value.get("id", "")) == "extension_diff" else "required_input"
        conditions.append(make_condition(
            condition_id, category, str(value.get("label") or value.get("id") or "Required input is missing"),
            source="stage", waivable=allow_documents and category in waivable_categories,
        ))
    for item in conditional_missing or []:
        value = item if isinstance(item, dict) else {"id": str(item), "label": str(item)}
        condition_id = str(value.get("condition_id") or f"missing:{value.get('id', 'conditional_input')}")
        conditions.append(make_condition(
            condition_id, "conditional_input", str(value.get("label") or value.get("id") or "Conditional input is absent"),
            source="stage", waivable=allow_documents and "conditional_input" in waivable_categories,
        ))
    for index, message in enumerate(errors or []):
        category = _condition_category(str(message), "required_input")
        stable_message = str(message).strip()
        condition_id = f"{category}:{re.sub(r'[^a-z0-9а-яё]+', '_', stable_message.casefold()).strip('_')[:100]}"
        conditions.append(make_condition(
            condition_id, category, stable_message, source="stage",
            waivable=allow_documents and category in waivable_categories and category not in HARD_NON_WAIVABLE_CATEGORIES,
        ))
    return list({item["id"]: item for item in conditions}.values())


def _stage_allows_deviation(stage_policy: dict, category: str, deviation_type: str = "process") -> bool:
    policy = stage_policy.get("deviation_policy", {}) if isinstance(stage_policy, dict) else {}
    return (
        bool(policy.get("allow_formal_documents"))
        and (category in set(policy.get("waivable_categories", []))
             or (deviation_type == "registry_bypass" and category == "registry_traceability"
                 and bool(stage_policy.get("provisional_allowed"))))
        and category not in HARD_NON_WAIVABLE_CATEGORIES
    )


def apply_deviation(gate: dict, decision: dict, stage_policy: dict) -> dict:
    """Apply one explicit user decision without mutating *gate*.

    The returned gate includes ``remaining_blockers``. Only condition IDs that
    were present in this gate can be waived; later reassessments therefore need
    a new user decision.
    """
    source = deepcopy(gate)
    decision = deepcopy(decision or {})
    reason = str(decision.get("reason") or decision.get("answer") or "").strip()
    statement = str(decision.get("user_statement") or reason).strip()
    deviation_type = str(decision.get("deviation_type") or "process")
    scope = str(decision.get("scope") or "gate")
    condition_ids = decision.get("condition_ids")
    if not reason or not statement:
        raise ValueError("A nonempty reason and user_statement are required")
    if deviation_type not in {"process", "registry_bypass"}:
        raise ValueError("Unknown deviation_type")
    if scope not in {"gate", "work-item"}:
        raise ValueError("Unknown deviation scope")
    if not isinstance(condition_ids, list) or not condition_ids or any(not str(item).strip() for item in condition_ids):
        raise ValueError("condition_ids must list the conditions explicitly accepted by the user")
    condition_ids = list(dict.fromkeys(str(item) for item in condition_ids))
    conditions = [item for item in source.get("conditions", []) if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in conditions}
    unknown = [item for item in condition_ids if item not in by_id]
    if unknown:
        raise ValueError("Unknown or no longer present condition ID: " + ", ".join(unknown))
    if source.get("operation") not in FORMAL_DOCUMENT_OPERATIONS:
        raise ValueError("Formal deviations are not available for this operation")
    if source.get("state") not in {"NEEDS_INPUT", "BLOCKED", "NEEDS_CONFIRMATION"}:
        raise ValueError("A deviation can only be recorded while the gate is waiting on a blocking condition")
    selected = [by_id[item] for item in condition_ids]
    if any(not item.get("waivable") or not _stage_allows_deviation(stage_policy, str(item.get("category")), deviation_type) for item in selected):
        raise ValueError("One or more selected conditions are non-waivable")
    if deviation_type == "process" and any(item.get("category") == "registry_traceability" for item in selected):
        raise ValueError("Generic process deviation cannot waive registry traceability")
    if deviation_type == "registry_bypass" and not any(item.get("category") == "registry_traceability" for item in selected):
        raise ValueError("registry_bypass must explicitly select a registry_traceability condition")
    if deviation_type == "registry_bypass" and not source.get("work_item_exists") and scope != "work-item":
        raise ValueError("registry_bypass for a missing work-item requires scope=work-item")
    fingerprint = (deviation_type, scope, tuple(sorted(condition_ids)), statement)
    existing = source.get("deviations", []) if isinstance(source.get("deviations"), list) else []
    previously_waived = {
        str(condition_id)
        for item in existing if isinstance(item, dict)
        for condition_id in item.get("condition_ids", item.get("waived_conditions", []))
    }
    effective_waivers = previously_waived | set(condition_ids)
    remaining = [
        item for item in conditions
        if item.get("blocking") and str(item.get("id")) not in effective_waivers
    ]
    duplicate = any((item.get("type"), item.get("scope"), tuple(sorted(item.get("waived_conditions", []))),
                     str(item.get("user_statement") or item.get("reason") or "").strip()) == fingerprint
                    for item in existing if isinstance(item, dict))
    if not duplicate:
        existing = [*existing, {
            "type": deviation_type, "actor": "user", "reason": reason,
            "user_statement": statement, "scope": scope, "condition_ids": condition_ids,
            "waived_conditions": condition_ids, "recorded_at": decision.get("recorded_at"),
        }]
    updated = deepcopy(source)
    updated["deviations"] = existing
    updated["remaining_blockers"] = remaining
    updated["compliance"] = "DEVIATED"
    updated["awaiting_user_input"] = bool(remaining)
    if remaining:
        updated["state"] = "BLOCKED"
        updated["user_message"] = "Отклонение принято только для разрешённых условий. Остались блокирующие условия: " + "; ".join(str(item.get("message")) for item in remaining)
    elif deviation_type == "registry_bypass" and not source.get("work_item_exists"):
        updated["state"] = "NEEDS_CONFIRMATION"
        updated["awaiting_user_input"] = False
        updated["user_message"] = "Обход реестра зафиксирован. Запустите provisional-start с пользовательским идентификатором и непустым описанием."
    else:
        updated["state"] = "READY_WITH_DEVIATIONS"
        updated["awaiting_user_input"] = False
        updated["user_message"] = "Отклонение зафиксировано. Можно продолжить; результат останется UNVERIFIED_DRAFT."
    return updated


def accept_deviation(gate: dict, *, reason: str, actor: str = "user",
                     deviation_type: str = "process", scope: str = "gate",
                     condition_ids: list[str] | None = None, user_statement: str = "",
                     stage_policy: dict | None = None) -> dict:
    """Compatibility wrapper for callers that already have a stage policy."""
    conditions = gate.get("conditions", [])
    if not conditions:
        # CLI callers use apply_deviation directly. This compatibility adapter
        # keeps older integrations that supplied only legacy missing_* fields
        # auditable without weakening the new typed contract.
        if deviation_type == "registry_bypass" and not gate.get("work_item_exists"):
            conditions = [{"id": "missing:registry_traceability", "category": "registry_traceability",
                           "message": "registry traceability is absent", "source": "registry",
                           "waivable": True, "blocking": True}]
        else:
            conditions = build_conditions(
                {"deviation_policy": {"allow_formal_documents": True, "waivable_categories": [
                    "required_input", "conditional_input", "status", "approval", "rlm_evidence", "diff_evidence", "bsl_evidence"]}},
                errors=list(gate.get("errors", [])), required_missing=list(gate.get("missing_required", [])),
                conditional_missing=list(gate.get("missing_conditional", [])),
            )
        gate = {**gate, "conditions": conditions}
    selected = condition_ids if condition_ids is not None else [str(item.get("id")) for item in conditions]
    policy = stage_policy or {"provisional_allowed": deviation_type == "registry_bypass",
        "deviation_policy": {"allow_formal_documents": True,
        "waivable_categories": ["required_input", "conditional_input", "status", "approval", "rlm_evidence", "diff_evidence", "bsl_evidence"]}}
    compatibility_scope = "work-item" if deviation_type == "registry_bypass" and not gate.get("work_item_exists") and scope == "gate" and not user_statement else scope
    return apply_deviation(gate, {"reason": reason, "actor": actor, "deviation_type": deviation_type,
                                  "scope": compatibility_scope, "condition_ids": selected, "user_statement": user_statement or reason}, policy)


def available_actions(*, mode: str, operation: str, state: str, stage: dict | None = None) -> list[str]:
    """Return actions that the state gate will actually accept."""
    if mode != "formal":
        return [] if state in TERMINAL else allowed_tools_for_mode(mode, operation)
    if state in TERMINAL:
        return []
    stage = stage or {}
    base = list(dict.fromkeys(stage.get("allowed_tools", [])))
    if state == "WAITING_USER":
        return ["flow1c_dialogue"]
    if state in {"NEEDS_INPUT", "BLOCKED", "NEEDS_CONFIRMATION", "NEEDS_CODE"}:
        recovery = [tool for tool in ("flow1c_intake", "flow1c_redmine_fetch", "flow1c_action", "flow1c_template") if tool in base]
        if operation == "code-review" and "flow1c_analyze_bsl" in base:
            recovery.append("flow1c_analyze_bsl")
        return ["flow1c_dialogue", *recovery]
    if state == "READY_WITH_DEVIATIONS" and operation in {"development", "publish"}:
        return [tool for tool in base if tool not in {"flow1c_write", "flow1c_action", "flow1c_complete"}]
    return base


def stage_errors(stage: dict, manifest: dict) -> list[str]:
    errors = []
    if manifest and stage.get("allowed_statuses") and manifest.get("status") not in stage["allowed_statuses"]:
        errors.append(f"status {manifest.get('status')} is not allowed; expected one of {', '.join(stage['allowed_statuses'])}")
    for role, expected in stage.get("required_approvals", {}).items():
        actual = manifest.get("approvals", {}).get(role)
        if actual != expected:
            errors.append(f"approval {role} is {actual or 'missing'}; expected {expected}")
    return errors


def readiness_state(errors: list, required_missing: list, conditional_missing: list, allow_incomplete: bool) -> str:
    if errors:
        return "BLOCKED"
    if required_missing or conditional_missing:
        # An incomplete formal gate is never authorized by a begin parameter.
        # The user must make a scoped decision through flow1c_dialogue first.
        return "NEEDS_INPUT"
    return "READY"


def assess_request(operation: str, mode: str, summary: str, *, code: str | None = None,
                   code_required: bool = False, mismatch: str = "") -> dict:
    """Return the next useful decision before expensive validation."""
    clarification = None
    state = "READY"
    if mode not in MODES:
        raise ValueError("Unknown work mode")
    if mode != "formal" and operation not in FREE_OPERATIONS and operation != "ambiguous":
        state = "BLOCKED"
        clarification = {"reason": "formal_action", "question": "Эта операция требует формального процесса. Можно обсудить её в режиме консультации.", "options": ["Обсудить задачу", "Перейти к формальному процессу"]}
    elif operation == "ambiguous" or not summary.strip():
        state = "WAITING_USER" if mode != "formal" else "NEEDS_CONFIRMATION"
        clarification = {"reason": "goal", "question": "Какой результат нужен: разобрать задачу или подготовить черновик документа?", "options": ["Разобрать задачу", "Подготовить черновик"]}
    elif mismatch:
        state = "WAITING_USER"
        clarification = {"reason": "requirement_mismatch", "detail": mismatch, "question": "Запрос расходится с требованиями указанного рабочего элемента. Исправим идентификатор или продолжим отдельным черновиком?", "options": ["Исправить идентификатор", "Отдельный черновик"]}
    elif mode == "formal" and code_required and not code:
        state = "NEEDS_CODE"
        clarification = {"reason": "reference", "question": "Укажите номер или идентификатор задачи. Если отдельного номера нет, можно использовать идентификатор проекта.", "options": ["Указать идентификатор", "Использовать идентификатор проекта", "Отдельный черновик"]}
    return {"mode": mode, "state": state, "clarification": clarification,
            "user_message": clarification["question"] if clarification else "Можно продолжать работу."}


def transition(gate: dict, action: str, *, question: str = "", answer: str = "") -> dict:
    """Return a new state; do not silently repeat a resolved question."""
    updated = {**gate}
    if action == "ask":
        if not question.strip():
            raise ValueError("A question is required")
        if any(item.get("question") == question for item in gate.get("answers", [])):
            raise ValueError("This question was already answered; use the saved answer")
        updated.update(state="WAITING_USER", resume_state=gate.get("resume_state", gate["state"]),
                       clarification={"reason": "user_input", "question": question, "options": []},
                       user_message=question, awaiting_user_input=True)
    elif action == "answer":
        if gate["state"] != "WAITING_USER" or not answer.strip():
            raise ValueError("A pending question and a nonempty answer are required")
        updated["answers"] = [*gate.get("answers", []), {"question": (gate.get("clarification") or {}).get("question", ""), "answer": answer}]
        updated.update(state=gate.get("resume_state", "READY"), clarification=None,
                       user_message="Ответ сохранён; продолжайте с учётом него.", awaiting_user_input=False)
        updated.pop("resume_state", None)
    else:
        raise ValueError("Unknown dialogue transition")
    return updated
