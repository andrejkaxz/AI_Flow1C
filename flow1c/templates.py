"""Template lifecycle with explicit paths and gate services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from flow1c import context as runtime
from flow1c import documents as documents
from flow1c import setup as setup_service
from flow1c import storage as storage
from flow1c import work_items as work_items
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import flow1c_policy as policy
from scripts import flow1c_sections_policy as sections_policy
from scripts import flow1c_templates as template_library
from scripts import flow1c_templates_policy as template_policy
from scripts import flow1c_templates_store as template_store

INPUT_FIELDS = (
    "operation_id",
    "source",
    "document_type",
    "template_id",
    "revision_id",
    "variant_name",
    "parent_revision",
    "resources_root",
    "offset",
    "limit",
    "profile",
    "expected_revision",
    "default",
    "pin",
    "documentation_path",
    "reason",
    "operations",
    "output_name",
    "plan_id",
    "text_offset",
    "guide_offset",
    "guide_limit",
    "metadata_offset",
)


def save_library_config(
    previous: dict, documentation: Path, library_id: str, *, product_root: Path
) -> None:
    """Merge library settings under a lock; never overwrite concurrent user fields."""
    path = product_root / runtime.LOCAL_CONFIG_FILE
    with template_store.exclusive(product_root / ".workspace/template-config"):
        current = storage.read_json(path, {})
        if current.get("schema_version", 0) > 2:
            raise template_policy.TemplateError(
                "TEMPLATE_INTEGRITY_FAILED",
                "Future local configuration preserved without rewriting.",
            )
        if current.get("documentation_path") != previous.get("documentation_path"):
            raise template_policy.TemplateError(
                "TEMPLATE_REVISION_CONFLICT",
                "Documentation configuration changed concurrently; refresh and resume.",
            )
        current_id = (current.get("template_library") or {}).get("library_id")
        if current_id and current_id != library_id:
            raise template_policy.TemplateError(
                "TEMPLATE_REVISION_CONFLICT",
                "Library identity changed concurrently; preserved without rewriting.",
            )
        storage.write_json(
            product_root
            / ".workspace/backups"
            / ("local-config-template-" + template_store.new_id() + ".json"),
            current,
        )
        current.update(schema_version=2, documentation_path=str(documentation))
        current.setdefault("template_library", {}).update(
            schema_version=1,
            product_root="document-templates",
            storage_kind="documentation",
            library_id=library_id,
        )
        template_store.atomic_json(path, current)


def document_root(
    gate: dict, service: template_library.TemplateService, *, product_root: Path
) -> Path:
    if gate.get("mode") == "draft" and gate.get("operation") == "template-document":
        return service.documentation / "drafts" / gate["gate_id"]
    if gate.get("mode") == "formal" and gate.get("operation") in {"functional-spec", "testing"}:
        reference = gate.get("work_reference") or gate.get("code")
        if not reference:
            raise template_policy.TemplateError(
                "TEMPLATE_POLICY_CONFLICT",
                "Formal template generation requires its existing work item.",
            )
        prefix = "specification" if gate["operation"] == "functional-spec" else "testing"
        return (
            work_items.work_item_root(reference, product_root=product_root)
            / prefix
            / "template-documents"
            / gate["gate_id"]
        )
    raise template_policy.TemplateError(
        "TEMPLATE_POLICY_CONFLICT",
        "Document tools require template-document draft or a specialized formal FS/testing gate.",
    )


def check_formal_content(
    gate: dict,
    service: template_library.TemplateService,
    request: dict,
    document_path: Path,
    *,
    product_root: Path,
) -> None:
    if gate.get("mode") != "formal":
        return
    if gate["state"] not in {"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}:
        raise template_policy.TemplateError(
            "TEMPLATE_POLICY_CONFLICT",
            "Resolve the current formal gate conditions before planning/writing.",
        )
    reference = gate.get("work_reference") or gate.get("code")
    _, manifest = work_items.load_manifest(reference, product_root=product_root)
    stage = runtime.load_stages(product_root=product_root)["operations"][gate["operation"]]
    if policy.stage_errors(stage, manifest):
        raise template_policy.TemplateError(
            "TEMPLATE_POLICY_CONFLICT",
            "Work-item status/approval prerequisites changed; reassess the formal gate.",
        )
    if gate["operation"] != "functional-spec":
        return
    pin = template_store.read_json(document_path / "template-pin.json")
    revision, revision_manifest = service.store.revision(pin)
    profile = template_store.read_json(revision / "profile.json")
    if revision_manifest["source_format"] != "docx" and "functional-spec" in (
        service.local.get("template_library", {}).get("require_word_for") or []
    ):
        raise template_policy.TemplateError(
            "TEMPLATE_POLICY_CONFLICT",
            "Project policy requires Word; select a DOCX variant in a new request.",
        )
    operations = request.get("operations")
    if operations is None:
        plan = template_store.read_json(
            document_path
            / "document-plans"
            / (template_store.identifier(request["plan_id"]) + ".json")
        )
        operations = plan["operations"]
    values = {op["target_id"]: op["value"] for op in operations}
    sections = {
        section["section_id"]: section
        for section in documents._sections_catalog(product_root=product_root)["sections"]
    }
    for section_id, mapped in profile.get("canonical_mapping", {}).items():
        _, approved_content, state = documents._load_section_current(
            documents._sections_root(reference, product_root=product_root),
            sections[section_id],
            None,
            reference,
        )
        content = "\n".join((str(values.get(i, "")) for i in mapped))
        sections_policy.ensure_write_authorized(state, content, write_command=True)
        if content.replace("\r\n", "\n") != approved_content.replace("\r\n", "\n"):
            raise template_policy.TemplateError(
                "TEMPLATE_POLICY_CONFLICT",
                "Formal content differs from the explicitly approved canonical section.",
            )


def handle(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    action = args.action
    if args.request is not None and (not isinstance(args.request, dict)):
        raise template_policy.TemplateError(
            "TEMPLATE_PROFILE_INCOMPLETE", "request must be a JSON object."
        )
    request = args.request or {}
    explicitly_supplied = getattr(args, "_json_input_fields", set())
    request = {
        **{
            k: getattr(args, k)
            for k in INPUT_FIELDS
            if getattr(args, k, None) is not None or k in explicitly_supplied
        },
        **request,
    }
    unknown = set(request) - set(INPUT_FIELDS)
    if unknown:
        raise template_policy.TemplateError(
            "TEMPLATE_PROFILE_INCOMPLETE",
            "Unknown template request fields: " + ", ".join(sorted(unknown)),
        )
    required = {
        "configure": ["documentation_path"],
        "intake": ["source", "document_type"],
        "profile-save": ["operation_id", "profile"],
        "activate": ["expected_revision"],
        "rollback": ["expected_revision"],
        "history": ["template_id"],
        "status": ["operation_id"],
        "resume": ["operation_id"],
        "resolve": ["document_type"],
        "relocate": ["documentation_path", "operation_id"],
        "document-plan": ["operations"],
        "document-write": ["plan_id"],
        "document-validate": ["plan_id"],
    }
    missing = [key for key in required.get(action, []) if key not in request]
    if missing or (
        action in {"activate", "rollback"}
        and (not (request.get("operation_id") or request.get("pin")))
    ):
        raise template_policy.TemplateError(
            "TEMPLATE_PROFILE_INCOMPLETE",
            "Required fields: " + ", ".join(missing or ["operation_id or pin"]),
        )
    gate = gate_state.load_gate(args.gate_id, product_root=product_root) if args.gate_id else None
    document_action = action in template_library.DOCUMENT_ACTIONS
    if action != "list" or gate:
        if gate is None:
            raise template_policy.TemplateError(
                "TEMPLATE_PROFILE_INCOMPLETE",
                "Open a template-management/template-document gate before this action.",
            )
        gate_state.require_gate_tool(
            gate,
            "flow1c_document" if document_action else "flow1c_template",
            product_root=product_root,
        )
        if gate["state"] in policy.TERMINAL:
            raise template_policy.TemplateError(
                "TEMPLATE_REVISION_STALE", "Start a new gate for a completed request."
            )
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    if local.get("schema_version", 0) > 2:
        raise template_policy.TemplateError(
            "TEMPLATE_INTEGRITY_FAILED",
            "Local configuration has a future schema; preserved without rewriting.",
        )
    if action == "configure":
        path = template_store.documentation_root(request["documentation_path"], product_root)
        existing = local.get("documentation_path")
        if existing and Path(existing).resolve() != path:
            raise template_policy.TemplateError(
                "TEMPLATE_REVISION_CONFLICT",
                "Use explicit relocate to change an already configured documentation folder.",
            )
        config_path = product_root / runtime.LOCAL_CONFIG_FILE
        with template_store.exclusive(product_root / ".workspace/template-config"):
            local = storage.read_json(config_path, {})
            if local.get("schema_version", 0) > 2:
                raise template_policy.TemplateError(
                    "TEMPLATE_INTEGRITY_FAILED",
                    "Future local configuration preserved without rewriting.",
                )
            if (
                local.get("documentation_path")
                and Path(local["documentation_path"]).resolve() != path
            ):
                raise template_policy.TemplateError(
                    "TEMPLATE_REVISION_CONFLICT",
                    "Documentation configuration changed concurrently; use relocate.",
                )
            if config_path.exists():
                backup = (
                    product_root
                    / ".workspace/backups"
                    / ("local-config-template-" + template_store.new_id() + ".json")
                )
                storage.write_json(backup, local)
            local.update(schema_version=2, documentation_path=str(path))
            local.setdefault(
                "template_library",
                {
                    "schema_version": 1,
                    "storage_kind": "documentation",
                    "root": "document-templates",
                    "library_id": None,
                },
            )
            template_store.atomic_json(config_path, local)
        result = {
            "schema_version": 1,
            "state": "CONFIGURED",
            "ready": True,
            "documentation_path": str(path),
            "storage_kind": "documentation",
        }
    elif action == "defer" and (not local.get("documentation_path")):
        result = {
            "schema_version": 1,
            "state": "DEFERRED",
            "ready": True,
            "operation_id": request.get("operation_id") or template_store.new_id(),
            "document_type": request.get("document_type"),
            "storage_kind": "checkpoint",
            "template_id": None,
            "revision_id": None,
            "reason": str(request.get("reason") or "user postponed template intake"),
        }
        if gate.get("operation") == "template-management":
            gate.update(state="READY", clarification=None)
    else:
        service = template_library.TemplateService(product_root, local)
        request_root = None
        if action == "resolve" or document_action:
            request_root = document_root(gate, service, product_root=product_root)
            if gate.get("mode") == "formal" and action == "resolve":
                expected_type = (
                    "functional-spec" if gate["operation"] == "functional-spec" else "test-protocol"
                )
                if request.get("document_type") != expected_type:
                    raise template_policy.TemplateError(
                        "TEMPLATE_POLICY_CONFLICT",
                        "Template type does not match the specialized formal operation.",
                    )
            if action in {"document-plan", "document-write"}:
                check_formal_content(
                    gate, service, request, request_root, product_root=product_root
                )
        result = service.dispatch(action, request, request_root)
        if action == "relocate":
            save_library_config(
                local,
                Path(request["documentation_path"]).resolve(),
                result["library_id"],
                product_root=product_root,
            )
            local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        if action == "intake" and local.get("template_library", {}).get("library_id") is None:
            save_library_config(
                local, service.documentation, result["library_id"], product_root=product_root
            )
            local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    if gate:
        gate.setdefault("template_operations", []).append(
            {
                "action": action,
                "operation_id": result.get("operation_id"),
                "state": result["state"],
                "template_id": result.get("template_id"),
                "revision_id": result.get("revision_id"),
            }
        )
        gate["storage_kind"] = "documentation" if local.get("documentation_path") else "workspace"
        if result.get("pin"):
            gate["template_pin"] = result["pin"]
        if action in {"document-write", "document-validate"} and result.get("ready"):
            gate["template_result"] = result
            if gate.get("mode") == "formal":
                evidence_path, evidence = gate_state.evidence_for_gate(
                    gate, product_root=product_root
                )
                output = Path(result["output"])
                relative = output.relative_to(
                    work_items.work_item_root(
                        gate.get("work_reference") or gate.get("code"), product_root=product_root
                    )
                ).as_posix()
                evidence.setdefault("changed_files", []).append(
                    {
                        "path": relative,
                        "sha256": result["output_sha256"],
                        "source": "template-document",
                    }
                )
                evidence["template_pin"] = result["pin"]
                storage.write_json(evidence_path, evidence)
        if action == "configure":
            gate.update(state="READY", clarification=None)
        if gate.get("operation") == "setup":
            gate.setdefault("template_progress", {})[
                result.get("operation_id") or "configuration"
            ] = {
                "state": result["state"],
                "template_id": result.get("template_id"),
                "revision_id": result.get("revision_id"),
                "document_type": result.get("document_type"),
                "questions": result.get("questions", []),
            }
            if gate.get("setup_id"):
                checkpoint = storage.read_json(
                    setup_service.setup_checkpoint_path(
                        gate["setup_id"], product_root=product_root
                    ),
                    {},
                )
                if checkpoint:
                    checkpoint["template_progress"] = gate["template_progress"]
                    setup_service.save_setup_checkpoint(checkpoint, product_root=product_root)
        gate_state.save_gate(gate, product_root=product_root)
    _value = result
    return OperationResult(_value, 2 if result["state"] == "FAILED_RECOVERABLE" else 0)


def command(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        return handle(args, product_root=product_root)
    except template_policy.TemplateError:
        raise
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise template_policy.TemplateError(
            (
                "TEMPLATE_STORAGE_UNAVAILABLE"
                if isinstance(exc, OSError)
                else "TEMPLATE_PROFILE_INCOMPLETE"
            ),
            f"Template action {args.action} could not finish ({type(exc).__name__}); saved state was preserved.",
            component="template-cli",
            next_action="Check the operation ID, input fields and library access, then resume.",
        ) from exc


def begin(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    operation = args.operation
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    mode = "draft" if operation == "template-document" else "explore"
    if args.mode != mode:
        raise template_policy.TemplateError(
            "TEMPLATE_POLICY_CONFLICT",
            f"{operation} requires {mode} mode; formal workflow routes remain separate.",
        )
    available = policy.allowed_tools_for_mode(mode, operation)
    gate = gate_state.new_gate(
        operation,
        None,
        "READY",
        mode=mode,
        summary=str(args.summary or ""),
        request_id=None,
        skill="flow1c-document-templates",
        available_actions=available,
        answers=[],
        notes=[],
        artifacts=[],
        mode_selection=args.mode_selection,
        storage_kind="documentation" if local.get("documentation_path") else "workspace",
        product_root=product_root,
    )
    gate["request_id"] = gate["gate_id"]
    skill = product_root / ".agents/skills/flow1c-document-templates/SKILL.md"
    gate["skill_instructions"] = skill.read_text(encoding="utf-8")
    try:
        template_store.documentation_root(str(local.get("documentation_path") or ""), product_root)
    except template_policy.TemplateError as exc:
        gate.update(
            state="WAITING_USER",
            clarification={
                "reason": "template_documentation_path",
                "question": "Укажите доступную папку пользовательской документации вне Flow1C либо отложите добавление шаблонов.",
            },
            next_action="template configure or defer",
            errors=[exc.as_dict()],
        )
    gate_state.save_gate(gate, product_root=product_root)
    _value = gate
    return OperationResult(_value, 0 if gate["state"] == "READY" else 1)


def complete(gate: dict, args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    if gate["operation"] == "template-management":
        operations = gate.get("template_operations", [])
        latest = {op["operation_id"]: op for op in operations if op.get("operation_id")}
        successful = [op for op in latest.values() if op["state"] in {"ACTIVE", "DEFERRED"}]
        if any((op["state"] not in {"ACTIVE", "DEFERRED"} for op in latest.values())):
            raise template_policy.TemplateError(
                "TEMPLATE_PROFILE_INCOMPLETE",
                "Resolve or explicitly defer every pending management operation before completion.",
            )
        if not successful:
            raise template_policy.TemplateError(
                "TEMPLATE_PROFILE_INCOMPLETE",
                "Activate a persisted profile/guide or explicitly defer before completing.",
            )
        if any((op["state"] == "ACTIVE" for op in successful)):
            service = template_library.TemplateService(
                product_root, storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
            )
            for op in successful:
                if op["state"] == "ACTIVE":
                    saved = service.store.operation(op["operation_id"])
                    service.store.revision(
                        {
                            k: saved[k]
                            for k in ("library_id", "template_id", "revision_id", "document_type")
                        }
                    )
        gate.update(state="CONSULTATION_COMPLETE")
    else:
        result = gate.get("template_result")
        if not result:
            raise template_policy.TemplateError(
                "TEMPLATE_PROFILE_INCOMPLETE",
                "Create and validate the pinned document before completing.",
            )
        service = template_library.TemplateService(
            product_root, storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        )
        result = service.document_validate(
            {"plan_id": result["plan_id"]}, service.documentation / "drafts" / gate["gate_id"]
        )
        gate.update(
            state="DRAFT_COMPLETE", document_status="UNVERIFIED_DRAFT", output=result["output"]
        )
    gate_state.save_gate(gate, product_root=product_root)
    _value = {
        "state": gate["state"],
        "output": gate.get("output"),
        "document_status": gate.get("document_status"),
        "ready": True,
    }
    return OperationResult(_value, 0)
