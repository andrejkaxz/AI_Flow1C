"""Thin shared gate adapter used by Codex, Claude and OpenCode."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from flow1c_templates import ACTIONS, DOCUMENT_ACTIONS, TemplateService
    from flow1c_templates_policy import TemplateError
    from flow1c_templates_store import atomic_json, documentation_root, exclusive, new_id, read_json as read_record, identifier
except ModuleNotFoundError:
    from scripts.flow1c_templates import ACTIONS, DOCUMENT_ACTIONS, TemplateService
    from scripts.flow1c_templates_policy import TemplateError
    from scripts.flow1c_templates_store import atomic_json, documentation_root, exclusive, new_id, read_json as read_record, identifier

INPUT_FIELDS = ("operation_id", "source", "document_type", "template_id", "revision_id", "variant_name", "parent_revision",
                "resources_root", "offset", "limit", "profile", "expected_revision", "default", "pin",
                "documentation_path", "reason", "operations", "output_name", "plan_id",
                "text_offset", "guide_offset", "guide_limit", "metadata_offset")


def save_library_config(api: Any, previous: dict, documentation: Path, library_id: str) -> None:
    """Merge library settings under a lock; never overwrite concurrent user fields."""
    path = api.ROOT / api.LOCAL_CONFIG_FILE
    with exclusive(api.ROOT / ".workspace/template-config"):
        current = api.read_json(path, {})
        if current.get("schema_version", 0) > 2:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Future local configuration preserved without rewriting.")
        if current.get("documentation_path") != previous.get("documentation_path"):
            raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Documentation configuration changed concurrently; refresh and resume.")
        current_id = (current.get("template_library") or {}).get("library_id")
        if current_id and current_id != library_id:
            raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Library identity changed concurrently; preserved without rewriting.")
        api.write_json(api.ROOT / ".workspace/backups" / ("local-config-template-" + new_id() + ".json"), current)
        current.update(schema_version=2, documentation_path=str(documentation))
        current.setdefault("template_library", {}).update(schema_version=1, root="document-templates",
                                                          storage_kind="documentation", library_id=library_id)
        atomic_json(path, current)


def document_root(api: Any, gate: dict, service: TemplateService) -> Path:
    if gate.get("mode") == "draft" and gate.get("operation") == "template-document":
        return service.documentation / "drafts" / gate["gate_id"]
    if gate.get("mode") == "formal" and gate.get("operation") in {"functional-spec", "testing"}:
        reference = gate.get("work_reference") or gate.get("code")
        if not reference:
            raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Formal template generation requires its existing work item.")
        prefix = "specification" if gate["operation"] == "functional-spec" else "testing"
        return api.work_item_root(reference) / prefix / "template-documents" / gate["gate_id"]
    raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Document tools require template-document draft or a specialized formal FS/testing gate.")


def check_formal_content(api: Any, gate: dict, service: TemplateService, request: dict, root: Path) -> None:
    if gate.get("mode") != "formal":
        return
    if gate["state"] not in {"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}:
        raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Resolve the current formal gate conditions before planning/writing.")
    reference = gate.get("work_reference") or gate.get("code")
    _, manifest = api.load_manifest(reference)
    stage = api.load_stages()["operations"][gate["operation"]]
    if api.stage_errors(stage, manifest):
        raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Work-item status/approval prerequisites changed; reassess the formal gate.")
    if gate["operation"] != "functional-spec":
        return
    pin = read_record(root / "template-pin.json")
    revision, revision_manifest = service.store.revision(pin)
    profile = read_record(revision / "profile.json")
    if revision_manifest["source_format"] != "docx" and "functional-spec" in (service.local.get("template_library", {}).get("require_word_for") or []):
        raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Project policy requires Word; select a DOCX variant in a new request.")
    operations = request.get("operations")
    if operations is None:
        plan = read_record(root / "document-plans" / (identifier(request["plan_id"]) + ".json"))
        operations = plan["operations"]
    values = {op["target_id"]: op["value"] for op in operations}
    sections = {section["section_id"]: section for section in api._sections_catalog()["sections"]}
    for section_id, mapped in profile.get("canonical_mapping", {}).items():
        _, approved_content, state = api._load_section_current(api._sections_root(reference), sections[section_id], None, reference)
        content = "\n".join(str(values.get(i, "")) for i in mapped)
        api.ensure_write_authorized(state, content, write_command=True)
        if content.replace("\r\n", "\n") != approved_content.replace("\r\n", "\n"):
            raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Formal content differs from the explicitly approved canonical section.")


def handle(api: Any, args: argparse.Namespace) -> int:
    action = args.action
    if args.request is not None and not isinstance(args.request, dict):
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "request must be a JSON object.")
    request = args.request or {}
    explicitly_supplied = getattr(args, "_json_input_fields", set())
    request = {**{k: getattr(args, k) for k in INPUT_FIELDS if getattr(args, k, None) is not None or k in explicitly_supplied}, **request}
    unknown = set(request) - set(INPUT_FIELDS)
    if unknown:
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Unknown template request fields: " + ", ".join(sorted(unknown)))
    required = {"configure": ["documentation_path"], "intake": ["source", "document_type"],
                "profile-save": ["operation_id", "profile"], "activate": ["expected_revision"],
                "rollback": ["expected_revision"], "history": ["template_id"], "status": ["operation_id"],
                "resume": ["operation_id"], "resolve": ["document_type"], "relocate": ["documentation_path", "operation_id"],
                "document-plan": ["operations"], "document-write": ["plan_id"], "document-validate": ["plan_id"]}
    missing = [key for key in required.get(action, []) if key not in request]
    if missing or (action in {"activate", "rollback"} and not (request.get("operation_id") or request.get("pin"))):
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Required fields: " + ", ".join(missing or ["operation_id or pin"]))
    gate = api.load_gate(args.gate_id) if args.gate_id else None
    document_action = action in DOCUMENT_ACTIONS
    if action != "list" or gate:
        if gate is None:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Open a template-management/template-document gate before this action.")
        api.require_gate_tool(gate, "flow1c_document" if document_action else "flow1c_template")
        if gate["state"] in api.TERMINAL:
            raise TemplateError("TEMPLATE_REVISION_STALE", "Start a new gate for a completed request.")
    local = api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {})
    if local.get("schema_version", 0) > 2:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Local configuration has a future schema; preserved without rewriting.")
    if action == "configure":
        path = documentation_root(request["documentation_path"], api.ROOT)
        existing = local.get("documentation_path")
        if existing and Path(existing).resolve() != path:
            raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Use explicit relocate to change an already configured documentation folder.")
        config_path = api.ROOT / api.LOCAL_CONFIG_FILE
        with exclusive(api.ROOT / ".workspace/template-config"):
            local = api.read_json(config_path, {})
            if local.get("schema_version", 0) > 2:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Future local configuration preserved without rewriting.")
            if local.get("documentation_path") and Path(local["documentation_path"]).resolve() != path:
                raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Documentation configuration changed concurrently; use relocate.")
            if config_path.exists():
                backup = api.ROOT / ".workspace/backups" / ("local-config-template-" + new_id() + ".json")
                api.write_json(backup, local)
            local.update(schema_version=2, documentation_path=str(path))
            local.setdefault("template_library", {"schema_version": 1, "storage_kind": "documentation", "root": "document-templates", "library_id": None})
            atomic_json(config_path, local)
        result = {"schema_version": 1, "state": "CONFIGURED", "ready": True, "documentation_path": str(path), "storage_kind": "documentation"}
    elif action == "defer" and not local.get("documentation_path"):
        result = {"schema_version": 1, "state": "DEFERRED", "ready": True,
                  "operation_id": request.get("operation_id") or new_id(), "document_type": request.get("document_type"),
                  "storage_kind": "checkpoint", "template_id": None, "revision_id": None,
                  "reason": str(request.get("reason") or "user postponed template intake")}
        if gate.get("operation") == "template-management":
            gate.update(state="READY", clarification=None)
    else:
        service = TemplateService(api.ROOT, local)
        request_root = None
        if action == "resolve" or document_action:
            request_root = document_root(api, gate, service)
            if gate.get("mode") == "formal" and action == "resolve":
                expected_type = "functional-spec" if gate["operation"] == "functional-spec" else "test-protocol"
                if request.get("document_type") != expected_type:
                    raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Template type does not match the specialized formal operation.")
            if action in {"document-plan", "document-write"}:
                check_formal_content(api, gate, service, request, request_root)
        result = service.dispatch(action, request, request_root)
        if action == "relocate":
            save_library_config(api, local, Path(request["documentation_path"]).resolve(), result["library_id"])
            local = api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {})
        if action == "intake" and local.get("template_library", {}).get("library_id") is None:
            save_library_config(api, local, service.documentation, result["library_id"])
            local = api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {})
    if gate:
        gate.setdefault("template_operations", []).append({"action": action, "operation_id": result.get("operation_id"),
            "state": result["state"], "template_id": result.get("template_id"), "revision_id": result.get("revision_id")})
        gate["storage_kind"] = "documentation" if local.get("documentation_path") else "workspace"
        if result.get("pin"):
            gate["template_pin"] = result["pin"]
        if action in {"document-write", "document-validate"} and result.get("ready"):
            gate["template_result"] = result
            if gate.get("mode") == "formal":
                evidence_path, evidence = api.evidence_for_gate(gate)
                output = Path(result["output"])
                relative = output.relative_to(api.work_item_root(gate.get("work_reference") or gate.get("code"))).as_posix()
                evidence.setdefault("changed_files", []).append({"path": relative, "sha256": result["output_sha256"], "source": "template-document"})
                evidence["template_pin"] = result["pin"]
                api.write_json(evidence_path, evidence)
        if action == "configure":
            gate.update(state="READY", clarification=None)
        if gate.get("operation") == "setup":
            gate.setdefault("template_progress", {})[result.get("operation_id") or "configuration"] = {
                "state": result["state"], "template_id": result.get("template_id"), "revision_id": result.get("revision_id"),
                "document_type": result.get("document_type"), "questions": result.get("questions", [])}
            if gate.get("setup_id"):
                checkpoint = api.read_json(api.setup_checkpoint_path(gate["setup_id"]), {})
                if checkpoint:
                    checkpoint["template_progress"] = gate["template_progress"]
                    api.save_setup_checkpoint(checkpoint)
        api.save_gate(gate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["state"] == "FAILED_RECOVERABLE" else 0


def register_parser(subparsers: Any, api: Any) -> None:
    template = subparsers.add_parser("template", help="Manage immutable project template revisions")
    template.add_argument("action", choices=ACTIONS + DOCUMENT_ACTIONS)
    template.add_argument("--json-stdin", action="store_true")
    template.add_argument("--gate-id")
    template.add_argument("--json", action="store_true")
    template.set_defaults(request=None, **{key: None for key in INPUT_FIELDS})
    template.set_defaults(handler=lambda args: command(api, args))
    for action in DOCUMENT_ACTIONS:
        document_parser = subparsers.add_parser(action, help="Write a document through a pinned template draft gate")
        document_parser.add_argument("--json-stdin", action="store_true")
        document_parser.add_argument("--gate-id")
        document_parser.set_defaults(action=action, request=None, **{key: None for key in INPUT_FIELDS})
        document_parser.set_defaults(handler=lambda args: command(api, args))


def command(api: Any, args: argparse.Namespace) -> int:
    try:
        return handle(api, args)
    except TemplateError:
        raise
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE" if isinstance(exc, OSError) else "TEMPLATE_PROFILE_INCOMPLETE",
                            f"Template action {args.action} could not finish ({type(exc).__name__}); saved state was preserved.",
                            component="template-cli", next_action="Check the operation ID, input fields and library access, then resume.") from exc


def begin(api: Any, args: argparse.Namespace) -> int:
    operation = args.operation
    local = api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {})
    mode = "draft" if operation == "template-document" else "explore"
    if args.mode != mode:
        raise TemplateError("TEMPLATE_POLICY_CONFLICT", f"{operation} requires {mode} mode; formal workflow routes remain separate.")
    available = api.allowed_tools_for_mode(mode, operation)
    gate = api.new_gate(operation, None, "READY", mode=mode, summary=str(args.summary or ""),
                        request_id=None, skill="flow1c-document-templates", available_actions=available,
                        answers=[], notes=[], artifacts=[], mode_selection=args.mode_selection,
                        storage_kind="documentation" if local.get("documentation_path") else "workspace")
    gate["request_id"] = gate["gate_id"]
    skill = api.ROOT / ".agents/skills/flow1c-document-templates/SKILL.md"
    gate["skill_instructions"] = skill.read_text(encoding="utf-8")
    try:
        documentation_root(str(local.get("documentation_path") or ""), api.ROOT)
    except TemplateError as exc:
        gate.update(state="WAITING_USER", clarification={"reason": "template_documentation_path",
            "question": "Укажите доступную папку пользовательской документации вне Flow1C либо отложите добавление шаблонов."},
            next_action="template configure or defer", errors=[exc.as_dict()])
    api.save_gate(gate)
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    return 0 if gate["state"] == "READY" else 1


def complete(api: Any, gate: dict, args: argparse.Namespace) -> int:
    if gate["operation"] == "template-management":
        operations = gate.get("template_operations", [])
        latest = {op["operation_id"]: op for op in operations if op.get("operation_id")}
        successful = [op for op in latest.values() if op["state"] in {"ACTIVE", "DEFERRED"}]
        if any(op["state"] not in {"ACTIVE", "DEFERRED"} for op in latest.values()):
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Resolve or explicitly defer every pending management operation before completion.")
        if not successful:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Activate a persisted profile/guide or explicitly defer before completing.")
        if any(op["state"] == "ACTIVE" for op in successful):
            service = TemplateService(api.ROOT, api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {}))
            for op in successful:
                if op["state"] == "ACTIVE":
                    saved = service.store.operation(op["operation_id"])
                    service.store.revision({k: saved[k] for k in ("library_id", "template_id", "revision_id", "document_type")})
        gate.update(state="CONSULTATION_COMPLETE")
    else:
        result = gate.get("template_result")
        if not result:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Create and validate the pinned document before completing.")
        service = TemplateService(api.ROOT, api.read_json(api.ROOT / api.LOCAL_CONFIG_FILE, {}))
        result = service.document_validate({"plan_id": result["plan_id"]}, service.documentation / "drafts" / gate["gate_id"])
        gate.update(state="DRAFT_COMPLETE", document_status="UNVERIFIED_DRAFT", output=result["output"])
    api.save_gate(gate)
    print(json.dumps({"state": gate["state"], "output": gate.get("output"), "document_status": gate.get("document_status"), "ready": True}, ensure_ascii=False))
    return 0
