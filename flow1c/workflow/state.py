"""Gate/request persistence, evidence and allowed tool checks."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import routing
from flow1c import storage as storage
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.routing_policy import ROUTE_POLICY_VERSION
from scripts import flow1c_git_policy as git_policy
from scripts import flow1c_policy as policy

AGENT_GATE_SCHEMA_VERSION = 1
POLICY_VERSION = 3


def meaningful_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if path.suffix.casefold() not in {".md", ".txt", ".yaml", ".json"}:
        return True
    if path.suffix.casefold() in {".yaml", ".json"}:
        try:
            return bool(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError):
            return False
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    table_separator_seen = False
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith(("#", "> Generated", "<!--", "> **UNVERIFIED_DRAFT")):
            continue
        if value.startswith("|"):
            compact = value.replace("|", "").replace("-", "").replace(":", "").strip()
            if not compact:
                table_separator_seen = True
                continue
            if table_separator_seen:
                return True
            continue
        if "{{" in value and "}}" in value:
            continue
        return True
    return False


def validate_json_record(value: Any, schema_path: Path) -> list[str]:
    """Validate the deliberately small schema subset used by FLOW1C agent records."""
    schema = storage.read_json(schema_path, {})
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return ["record or schema is not a JSON object"]
    errors: list[str] = []
    for key in schema.get("required", []):
        if key not in value:
            errors.append(f"missing required property {key}")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    for key, rules in schema.get("properties", {}).items():
        if key not in value or not isinstance(rules, dict):
            continue
        current = value[key]
        expected = rules.get("type")
        names = expected if isinstance(expected, list) else [expected] if expected else []
        expected_types = tuple((type_map[name] for name in names if name in type_map))
        if expected_types and (not isinstance(current, expected_types) or
                               isinstance(current, bool) and "integer" in names):
            errors.append(f"property {key} has an invalid type")
            continue
        if "const" in rules and current != rules["const"]:
            errors.append(f"property {key} must equal {rules['const']}")
        if "enum" in rules and current not in rules["enum"]:
            errors.append(f"property {key} is outside the allowed enum")
        if (
            isinstance(current, str)
            and rules.get("pattern")
            and (not re.fullmatch(str(rules["pattern"]), current))
        ):
            errors.append(f"property {key} does not match {rules['pattern']}")
        if (
            isinstance(current, str)
            and rules.get("minLength")
            and (len(current) < int(rules["minLength"]))
        ):
            errors.append(f"property {key} is shorter than {rules['minLength']}")
        if (
            isinstance(current, str)
            and rules.get("maxLength")
            and (len(current) > int(rules["maxLength"]))
        ):
            errors.append(f"property {key} is longer than {rules['maxLength']}")
    return errors


def gate_path(gate_id: str, *, product_root: Path) -> Path:
    if not re.fullmatch("[a-f0-9-]{8,64}", str(gate_id or ""), flags=re.IGNORECASE):
        raise WorkflowError("Invalid agent gate ID.")
    return product_root / ".workspace" / "agent-gates" / f"{gate_id}.json"


def save_gate(gate: dict[str, Any], *, product_root: Path) -> None:
    sanitized = storage.sanitize_json_value(gate)
    gate.clear()
    gate.update(sanitized)
    storage.write_json(gate_path(str(gate["gate_id"]), product_root=product_root), gate)


def refresh_gate_actions(
    gate: dict[str, Any], *, persist: bool = False, product_root: Path
) -> dict[str, Any]:
    """Recompute the executable tool surface for the current gate state."""
    stage = (
        runtime.load_stages(product_root=product_root)
        .get("operations", {})
        .get(str(gate.get("operation")), {})
    )
    actions = policy.available_actions(
        mode=str(gate.get("mode", "formal")),
        operation=str(gate.get("operation", "")),
        state=str(gate.get("state", "")),
        stage=stage if isinstance(stage, dict) else {},
    )
    if gate.get("available_actions") != actions:
        gate["available_actions"] = actions
        if persist:
            save_gate(gate, product_root=product_root)
    return gate


def load_gate(
    gate_id: str, *, states: set[str] | None = None, product_root: Path
) -> dict[str, Any]:
    gate = storage.read_json(gate_path(gate_id, product_root=product_root))
    if not isinstance(gate, dict) or gate.get("schema_version") != AGENT_GATE_SCHEMA_VERSION:
        raise WorkflowError("Agent gate does not exist or has an unsupported schema.")
    schema_errors = validate_json_record(gate, product_root / "schemas" / "agent-gate.schema.json")
    if schema_errors:
        raise WorkflowError("Agent gate schema validation failed: " + "; ".join(schema_errors))
    if states is not None and gate.get("state") not in states:
        raise WorkflowError(f"Agent gate state {gate.get('state')} is not allowed for this action.")
    if gate.get("policy_version") != POLICY_VERSION:
        raise WorkflowError(
            "Legacy gate requires re-assessment: call flow1c_begin with the saved operation, code and summary. Existing evidence is preserved."
        )
    route_changed = False
    saved_decision = gate.get("route_decision")
    if saved_decision is not None and (
        not isinstance(saved_decision, dict)
        or type(saved_decision.get("schema_version")) is not int
        or saved_decision["schema_version"] != 1
        or type(saved_decision.get("policy_version")) is not int
        or saved_decision["policy_version"] != ROUTE_POLICY_VERSION
    ):
        raise WorkflowError("ROUTE_RECOVERY_REQUIRED: unsupported saved RouteDecision version; state is preserved")
    if gate.get("route_origin") == "structured" and "route_proposal" not in gate:
        raise WorkflowError("ROUTE_RECOVERY_REQUIRED: structured gate has no saved proposal")
    if "route_proposal" in gate:
        decision = routing.check_proposal(gate["route_proposal"], product_root=product_root)
        if decision["status"] != "VALID" or any(
            decision[field] != gate.get(target) for field, target in (
                ("operation", "operation"), ("mode", "mode"), ("primary_skill", "skill"),
            )
        ):
            raise WorkflowError(
                "ROUTE_RECOVERY_REQUIRED: saved proposal is incompatible with the current gate; "
                "preserve answers/evidence and reassess the route before continuing."
            )
        route_changed = gate.get("route_decision") != decision
        gate["route_decision"] = decision
    refresh_gate_actions(gate, persist=True, product_root=product_root)
    if route_changed:
        save_gate(gate, product_root=product_root)
    return gate


def require_gate_tool(
    gate: dict[str, Any], tool_name: str, *, product_root: Path
) -> dict[str, Any]:
    if gate.get("mode", "formal") != "formal":
        allowed = policy.allowed_tools_for_mode(
            str(gate.get("mode")), str(gate.get("operation", ""))
        )
        if tool_name not in allowed:
            raise WorkflowError(f"Tool {tool_name} is unavailable in {gate.get('mode')} mode")
        return {
            "allowed_tools": allowed,
            "writable_targets": ["draft"] if gate.get("mode") == "draft" else [],
        }
    stages = runtime.load_stages(product_root=product_root)
    stage = stages.get("operations", {}).get(gate.get("operation"))
    actions = policy.available_actions(
        mode="formal",
        operation=str(gate.get("operation")),
        state=str(gate.get("state")),
        stage=stage if isinstance(stage, dict) else {},
    )
    if tool_name not in actions:
        raise WorkflowError(
            f"Tool {tool_name} is not allowed for operation {gate.get('operation')}."
        )
    return {**(stage if isinstance(stage, dict) else {}), "allowed_tools": actions}


def new_gate(
    operation: str, code: str | None, state: str, *, product_root: Path, **values: Any
) -> dict[str, Any]:
    gate = {
        "schema_version": AGENT_GATE_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "mode": "formal",
        "gate_id": str(uuid.uuid4()),
        "operation": operation,
        "code": code,
        "compliance": "COMPLIANT",
        "deviations": [],
        "state": state,
        "created_at": storage.utc_now(),
        **values,
    }
    stage = runtime.load_stages(product_root=product_root)["operations"].get(operation, {})
    mode = str(gate.get("mode", "formal"))
    if gate.get("route_decision", {}).get("status") == "VALID":
        gate.setdefault("skill", gate["route_decision"]["primary_skill"])
    gate.setdefault("conditions", [])
    gate.setdefault("remaining_blockers", [])
    gate.setdefault(
        "available_actions",
        policy.available_actions(mode=mode, operation=operation, state=state, stage=stage),
    )
    gate.setdefault("clarification", None)
    if state in {"NEEDS_CODE", "NEEDS_INPUT", "NEEDS_CONFIRMATION", "BLOCKED"} and (
        not gate["clarification"]
    ):
        gate["clarification"] = {
            "reason": state.lower(),
            "question": gate.get("user_message", "Уточните следующий шаг."),
            "options": ["Предоставить данные", "Обсудить отдельно", "Независимый черновик"],
        }
    gate.setdefault(
        "awaiting_user_input",
        bool(
            gate.get("clarification")
            and state
            in {"NEEDS_CODE", "NEEDS_INPUT", "NEEDS_CONFIRMATION", "WAITING_USER", "BLOCKED"}
        ),
    )
    refresh_gate_actions(gate, product_root=product_root)
    save_gate(gate, product_root=product_root)
    return gate


def request_root(gate: dict[str, Any], *, product_root: Path) -> Path:
    request_id = str(gate.get("request_id", gate["gate_id"]))
    if not re.fullmatch("[a-f0-9-]{8,64}", request_id):
        raise WorkflowError("Invalid request ID")
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    if gate.get("storage_kind") == "workspace":
        base = product_root / ".workspace" / "drafts"
    else:
        base = (
            runtime.project_root(local, product_root=product_root) / "drafts"
            if local.get("documentation_path")
            else product_root / ".workspace" / "drafts"
        )
    return (base / request_id).resolve()


def save_request(gate: dict[str, Any], *, product_root: Path) -> None:
    save_gate(gate, product_root=product_root)
    if gate.get("mode", "formal") != "formal":
        storage.write_json(
            request_root(gate, product_root=product_root) / "request.json",
            {**gate, "generated_by": "Flow1C", "document_status": "UNVERIFIED_DRAFT"},
        )


def safe_request_file(root: Path, relative: str, *, markdown: bool = False) -> Path:
    target = (root / relative).resolve()
    if not relative or not storage.path_is_within(target, root) or target == root.resolve():
        raise WorkflowError("Path is outside the request directory")
    if markdown and (
        target.suffix.lower() != ".md"
        or target.relative_to(root.resolve()).parts[0] in {"sources", "evidence"}
    ):
        raise WorkflowError("Draft outputs must be Markdown outside source/evidence directories")
    return target


def evidence_for_gate(gate: dict[str, Any], *, product_root: Path) -> tuple[Path, dict[str, Any]]:
    raw = str(gate.get("evidence_path", "")).strip()
    if not raw:
        raise WorkflowError("This gate has no evidence record.")
    path = Path(raw).resolve()
    expected_root = (
        request_root(gate, product_root=product_root)
        if gate.get("mode", "formal") != "formal"
        else (
            work_items.work_item_root(
                str(gate.get("work_reference") or gate.get("code")), product_root=product_root
            )
            / "evidence"
        ).resolve()
    )
    if not storage.path_is_within(path, expected_root):
        raise WorkflowError("Evidence path is outside the current work item.")
    evidence = storage.read_json(path)
    if not isinstance(evidence, dict):
        raise WorkflowError("Evidence record is missing or invalid.")
    if evidence.get("gate_id") and evidence["gate_id"] != gate["gate_id"]:
        raise WorkflowError("Evidence belongs to a different gate")
    version = evidence.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise WorkflowError("Evidence record has an unsupported schema version; saved evidence is preserved.")
    if version == 1:
        evidence["schema_version"] = 2
        migrated = []
        for item in evidence.get("git_analysis", []):
            if isinstance(item, dict) and isinstance(item.get("result"), dict):
                item = {**item, "result": git_policy.migrate_record(item["result"])}
            migrated.append(item)
        evidence["git_analysis"] = migrated
        evidence.setdefault("snapshots", [])
    return (path, evidence)


def recoverable_update_action_gate(gate: dict[str, Any]) -> bool:
    """Accept one retry for an update closed by an unrecorded successful action."""
    return (
        gate.get("state") == "NON_COMPLIANT"
        and gate.get("mode", "formal") == "formal"
        and (gate.get("operation") == "update")
        and (not gate.get("action_completed"))
        and (gate.get("completion_errors") == ["the requested workflow action has not completed"])
    )
