"""Pure checks for structured route proposals; decisions never grant permissions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from scripts import flow1c_policy as workflow_policy
from scripts import flow1c_query_policy as query_policy

ROUTE_POLICY_VERSION = 1
MAX_PROPOSAL_BYTES = 32_768
SOURCE_KINDS = (
    "chat", "attachment", "configuration", "extension", "git_history",
    "git_snapshot", "workflow", "registry", "work_item", "template_library", "redmine",
)
SOURCE_VERSIONS = ("provided", "current", "historical")
PROPOSAL_FIELDS = {
    "schema_version", "expected_outcome", "operation", "mode", "primary_skill", "role",
    "sources", "source_relation", "references", "gate_id", "request_id", "basis", "ambiguities", "substeps",
}


class RoutingError(ValueError):
    """Catalog failures preserve all state and require a compatible installation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "ROUTE_CATALOG_INVALID"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "component": "routing-catalog", "message": str(self),
                "recoverable": True, "preserved_state": True,
                "next_actions": ["restore-compatible-product-configuration"]}


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_rules(catalog: dict[str, Any], stages: dict[str, Any],
                skill_ids: set[str]) -> dict[str, Any]:
    """Compose catalog references with their owners, without reading user settings."""
    if not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise RoutingError("Unsupported route catalog version.")
    if set(catalog) != {"schema_version", "routes", "source_rules", "substeps", "compatibility"}:
        raise RoutingError("Unknown or missing catalog properties.")
    if not isinstance(stages, dict) or type(stages.get("version")) is not int or stages["version"] != 1 or not isinstance(stages.get("operations"), dict):
        raise RoutingError("Unsupported stage catalog.")
    operations = stages["operations"]
    routes = catalog.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RoutingError("A nonempty route list is required.")
    if catalog.get("source_rules") != source_rules():
        raise RoutingError("Source rules do not match the supported source boundary.")
    expected_substeps = {
        "redmine-files": {"tool": "flow1c_redmine_files", "pre_gate": True, "access": "read-only"},
        "redmine-relations": {"tool": "flow1c_redmine_relations", "pre_gate": True, "access": "read-only"},
        "template-library": {"tool": "flow1c_template", "pre_gate": False, "access": "gated"},
    }
    if catalog.get("substeps") != expected_substeps:
        raise RoutingError("Unrecognized substep boundary.")
    if catalog.get("compatibility") != compatibility_map():
        raise RoutingError("Compatibility map differs from published legacy inputs.")
    effective: dict[str, Any] = {}
    route_ids: set[str] = set()
    for entry in routes:
        if not isinstance(entry, dict) or set(entry) != {
            "route_id", "intent_family", "operation", "description", "activation", "exclusions",
            "boundary_examples", "modes", "free_primary_skill", "stage_policy", "sources",
            "substeps", "output_contract", "clarification",
        }:
            raise RoutingError("Route properties do not match catalog v1.")
        operation = entry["operation"]
        route_id = entry["route_id"]
        if not isinstance(operation, str) or operation not in operations or operation in effective:
            raise RoutingError("Unknown or duplicate operation.")
        if not isinstance(route_id, str) or not route_id.strip() or route_id in route_ids:
            raise RoutingError("Missing or duplicate route ID.")
        route_ids.add(route_id)
        for field in ("intent_family", "description", "output_contract"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise RoutingError(f"Route {operation} requires {field}.")
        output = entry["output_contract"]
        if not output.startswith(("docs/", "schemas/")) or "\\" in output or any(
            part in {"", ".", ".."} for part in output.split("/")
        ):
            raise RoutingError(f"Output contract must reference product docs/schemas: {operation}.")
        for field in ("activation", "exclusions", "boundary_examples"):
            if not isinstance(entry[field], list) or not entry[field] or any(
                not isinstance(item, str) or not item.strip() for item in entry[field]
            ):
                raise RoutingError(f"Route {operation} requires nonempty {field} examples.")
        modes = entry["modes"]
        if not isinstance(modes, list) or not modes or any(
            not isinstance(mode, str) or mode not in workflow_policy.MODES for mode in modes
        ) or len(set(modes)) != len(modes):
            raise RoutingError(f"Invalid modes for {operation}.")
        if any(mode != "formal" for mode in modes) and operation not in workflow_policy.FREE_OPERATIONS:
            raise RoutingError(f"Free mode is unsupported by the operation owner: {operation}.")
        if operation in {"consultation", "query-analysis", "workflow-review", "interview-preparation"} and "formal" in modes:
            raise RoutingError(f"The existing begin service rejects formal {operation}.")
        if entry["stage_policy"] != operation:
            raise RoutingError(f"Stage reference must point to its operation: {operation}.")
        if not isinstance(entry["sources"], list) or not entry["sources"] or any(
            not isinstance(kind, str) or kind not in SOURCE_KINDS for kind in entry["sources"]
        ) or len(set(entry["sources"])) != len(entry["sources"]):
            raise RoutingError(f"Invalid source kinds for {operation}.")
        if not isinstance(entry["substeps"], list) or any(
            not isinstance(step, str) or step not in expected_substeps for step in entry["substeps"]
        ) or len(set(entry["substeps"])) != len(entry["substeps"]):
            raise RoutingError(f"Unknown substep for {operation}.")
        question = entry["clarification"]
        if (not isinstance(question, dict) or set(question) != {"question", "options"}
            or not isinstance(question["question"], str) or not question["question"].strip()
            or not isinstance(question["options"], list) or not 2 <= len(question["options"]) <= 3
            or any(not isinstance(item, str) or not item.strip() for item in question["options"])
            or len(set(question["options"])) != len(question["options"])):
            raise RoutingError(f"Invalid clarification for {operation}.")
        stage = operations[operation]
        primary = {mode: stage["skill"] if mode == "formal" else entry["free_primary_skill"]
                   for mode in modes}
        if any(skill not in skill_ids for skill in primary.values()):
            raise RoutingError(f"Primary skill is unavailable: {operation}.")
        roles = {mode: stage.get("context_role") if mode == "formal" else None for mode in modes}
        if any(role is not None and role not in {
            "analyst", "functional-architect", "technical-architect", "tester",
        } for role in roles.values()):
            raise RoutingError(f"Unsupported role: {operation}.")
        effective[operation] = {**deepcopy(entry), "primary_skills": primary, "roles": roles}
    if set(effective) != set(operations):
        raise RoutingError("The catalog must cover every public stage operation exactly once.")
    rules = {
        "schema_version": 1, "policy_version": ROUTE_POLICY_VERSION,
        "routes": effective, "source_rules": source_rules(), "substeps": deepcopy(expected_substeps),
        "compatibility": compatibility_map(),
        # Include owner rules so a changed stage/allowlist cannot retain an old route digest.
        "owners": {"stages": deepcopy(stages), "free_tools": list(workflow_policy.FREE_TOOLS),
                   "draft_tools": list(workflow_policy.DRAFT_TOOLS)},
    }
    rules["digest"] = fingerprint(rules)
    return rules


def source_rules() -> dict[str, Any]:
    """Requested versions are resolved later by gated source/evidence tools."""
    versions = {
        "chat": "provided", "attachment": "provided", "configuration": "current",
        "extension": "current", "git_history": "historical", "git_snapshot": "historical",
        "workflow": "current", "registry": "current", "work_item": "current",
        "template_library": "current", "redmine": "current",
    }
    resolvers = {
        "chat": "conversation", "attachment": "accepted-intake", "configuration": "gated-rlm",
        "extension": "gated-rlm", "git_history": "gated-git-inspect",
        "git_snapshot": "gated-git-snapshot-and-rlm", "workflow": "bounded-workflow-inspect",
        "registry": "registry-import", "work_item": "work-reference-resolver",
        "template_library": "pinned-template-revision", "redmine": "redmine-provenance",
    }
    return {kind: {"version": versions[kind], "resolver": resolvers[kind]} for kind in SOURCE_KINDS}


def compatibility_map() -> dict[str, Any]:
    return {
        "operation_aliases": {},
        "reference_fields": {"code": "task_reference", "g_number": "task_reference"},
        "git_actions": {"integration": "merge-search"},
        "defaults_owner": "scripts.flow1c_policy.select_request_mode",
        "query_remap_owner": "scripts.flow1c_query_policy.infer_query_request_intent",
    }


def legacy_proposal(operation: str, summary: str, mode: str | None = None) -> dict[str, Any]:
    """Reproduce published defaults/remapping; never guess an unknown operation."""
    operation = operation.strip().casefold()
    if operation == "consultation" and mode != "draft" and query_policy.infer_query_request_intent(summary):
        operation = "query-analysis"
    selected = workflow_policy.select_request_mode(operation, summary, mode)
    return {"schema_version": 1, "operation": operation, "mode": selected["mode"],
            "expected_outcome": summary, "sources": []}


def proposal_errors(proposal: Any) -> list[str]:
    """Validate the bounded domain contract using only the standard library."""
    if not isinstance(proposal, dict):
        return ["proposal must be an object"]
    try:
        size = len(json.dumps(proposal, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return ["proposal must contain finite UTF-8 JSON values"]
    if size > MAX_PROPOSAL_BYTES:
        return ["proposal exceeds 32768 bytes"]
    errors = []
    if set(proposal) - PROPOSAL_FIELDS:
        errors.append("unknown proposal properties")
    if type(proposal.get("schema_version")) is not int or proposal["schema_version"] != 1:
        errors.append("unsupported proposal version")
    if not isinstance(proposal.get("expected_outcome"), str) or len(proposal["expected_outcome"]) > 4000:
        errors.append("expected_outcome must be a string of at most 4000 characters")
    if proposal.get("source_relation", "single") not in ("single", "compare"):
        errors.append("invalid source_relation")
    for field in ("operation", "mode", "primary_skill", "role", "gate_id", "request_id"):
        value = proposal.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
            errors.append(f"invalid {field}")
    references = proposal.get("references", {})
    if not isinstance(references, dict) or set(references) - {"task_reference", "project_reference", "git_ref"}:
        errors.append("invalid references")
    else:
        for field, value in references.items():
            try:
                if value is not None and (not isinstance(value, str) or not value.strip() or
                    len(value) > (256 if field == "git_ref" else 128) or workflow_policy.CONTROL_PATTERN.search(value)):
                    raise ValueError("reference must be a nonempty bounded string")
                (workflow_policy.validate_git_ref if field == "git_ref" else workflow_policy.validate_reference)(value)
            except ValueError:
                errors.append(f"invalid reference {field}")
    for field, maximum in (("sources", 8), ("basis", 16), ("ambiguities", 5), ("substeps", 3)):
        values = proposal.get(field, [])
        if not isinstance(values, list) or len(values) > maximum:
            errors.append(f"invalid {field}")
            continue
        for item in values:
            if field in {"basis", "substeps", "ambiguities"}:
                if not isinstance(item, str) or not item.strip() or len(item) > 256:
                    errors.append(f"invalid {field} item")
                elif field == "ambiguities" and item not in {"operation", "mode", "source", "object", "outcome"}:
                    errors.append("unknown ambiguity field")
            elif (not isinstance(item, dict) or set(item) - {"kind", "selector", "version", "repository"}
                  or item.get("kind") not in SOURCE_KINDS or item.get("version") not in SOURCE_VERSIONS
                  or ("repository" in item and (item.get("kind") not in {"git_history", "git_snapshot"}
                      or item["repository"] not in ("workflow", "extension")))
                  or ("selector" in item and (not isinstance(item["selector"], str)
                      or not item["selector"].strip() or len(item["selector"]) > 256))):
                errors.append("invalid source descriptor; only requested source fields are allowed")
    return errors


def clarification_for(fields: list[str], route: dict[str, Any] | None) -> dict[str, Any]:
    """Ask about the first unresolved choice, without combining unrelated questions."""
    field = fields[0]
    if field == "source":
        return {"question": "По каким источникам нужно выполнить проверку или сравнение?",
                "options": ["Текущие источники", "Историческая версия", "Предоставленные материалы"]}
    if field == "object":
        return {"question": "Какой конкретный объект, файл или Git ref нужно проверить?",
                "options": ["Указать объект или файл", "Указать ветку или коммит"]}
    if field == "mode" and route:
        labels = {"explore": "Обсудить или проверить", "draft": "Подготовить черновик",
                  "formal": "Выполнить формальный этап"}
        options = [labels[mode] for mode in route["modes"]]
        if len(options) == 1:
            options.append("Уточнить цель запроса")
        return {"question": "В каком режиме выполнить эту операцию?", "options": options}
    return deepcopy(route["clarification"] if route else {
        "question": "Какой результат нужен?",
        "options": ["Объяснить подход", "Проверить объект", "Подготовить документ"],
    })


def check_route(proposal: Any, rules: dict[str, Any]) -> dict[str, Any]:
    """Check fields and source versions. Natural language interpretation is the adapter's job."""
    decision: dict[str, Any] = {
        "schema_version": 1, "policy_version": ROUTE_POLICY_VERSION,
        "catalog_digest": rules["digest"], "status": "INVALID", "requested_operation": None,
        "route_id": None, "operation": None, "mode": None, "primary_skill": None, "role": None,
        "sources": [], "source_relation": "single", "substeps": [], "references": {}, "basis": [],
        "reason_codes": [], "unresolved_fields": [], "clarification": None,
        "errors": [], "next_actions": [],
    }
    errors = proposal_errors(proposal)
    if not errors:
        operation, mode = proposal.get("operation"), proposal.get("mode")
        decision["requested_operation"] = operation
        route = rules["routes"].get(operation)
        unresolved = list(dict.fromkeys(proposal.get("ambiguities", [])))
        if not operation:
            unresolved.append("operation")
        elif route is None:
            errors.append("unknown operation; consultation fallback is forbidden")
        if not mode:
            unresolved.append("mode")
        elif mode not in workflow_policy.MODES or (route and mode not in route["modes"]):
            errors.append("mode is unsupported by this route")
        if not proposal["expected_outcome"].strip():
            unresolved.append("outcome")
        if route and mode in route["modes"]:
            skill, role = route["primary_skills"][mode], route["roles"][mode]
            if "primary_skill" in proposal and proposal["primary_skill"] != skill:
                errors.append("primary_skill differs from the operation/mode owner")
            if "role" in proposal and proposal["role"] != role:
                errors.append("role differs from the operation/mode owner")
            sources = proposal.get("sources", [])
            source_relation = proposal.get("source_relation", "single")
            if source_relation == "compare" and len({fingerprint(item) for item in sources}) < 2:
                unresolved.append("source")
            resolved_sources = []
            for source in sources:
                kind = source["kind"]
                rule = rules["source_rules"][kind]
                if kind not in route["sources"] or source["version"] != rule["version"]:
                    errors.append("source kind/version is incompatible with the route")
                if kind in {"git_history", "git_snapshot", "attachment", "redmine"} and not source.get("selector"):
                    unresolved.append("object")
                resolved_sources.append({**deepcopy(source), "resolver": rule["resolver"]})
            substeps = proposal.get("substeps", [])
            if any(step not in route["substeps"] for step in substeps):
                errors.append("substep is outside the route catalog")
            decision.update(route_id=route["route_id"], operation=operation, mode=mode,
                            primary_skill=skill, role=role, sources=resolved_sources, source_relation=source_relation,
                            substeps=list(dict.fromkeys(substeps)),
                            references=deepcopy(proposal.get("references", {})),
                            basis=list(proposal.get("basis", [])))
        if not errors:
            decision["unresolved_fields"] = list(dict.fromkeys(unresolved))
            if unresolved:
                decision.update(status="CLARIFICATION_REQUIRED", reason_codes=["ROUTE_AMBIGUOUS"],
                                next_actions=["clarify-before-gate"])
                decision["clarification"] = clarification_for(decision["unresolved_fields"], route)
            else:
                decision.update(status="VALID", reason_codes=["STRUCTURED_PROPOSAL"],
                                next_actions=["begin-with-current-gate-checks"])
    if errors:
        decision["reason_codes"] = ["ROUTE_INVALID_COMBINATION"]
        decision["errors"] = [{"code": "ROUTE_INVALID_COMBINATION", "component": "routing-policy",
                               "message": message, "recoverable": True, "preserved_state": True,
                               "next_actions": ["correct-proposal"]} for message in errors]
        decision["next_actions"] = ["correct-proposal"]
    decision["fingerprint"] = fingerprint(decision)
    return decision


def check_legacy_route(operation: str, summary: str, mode: str | None,
                       rules: dict[str, Any]) -> dict[str, Any]:
    """Comparison-only bridge for defaults and the existing narrow query remap."""
    proposed = legacy_proposal(operation, summary, mode)
    decision = check_route(proposed, rules)
    decision["requested_operation"] = operation.strip().casefold()
    if decision["status"] == "VALID":
        selection = workflow_policy.select_request_mode(proposed["operation"], summary, mode)
        decision["reason_codes"] = ["LEGACY_" + selection["reason"].upper()]
        if proposed["operation"] != decision["requested_operation"]:
            decision["reason_codes"].append("EXPLICIT_1C_QUERY_REQUEST")
    decision.pop("fingerprint")
    decision["fingerprint"] = fingerprint(decision)
    return decision
