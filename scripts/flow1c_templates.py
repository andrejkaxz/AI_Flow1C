"""Template orchestration. Policy, storage and format mechanics are separate modules."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    from flow1c_templates_policy import TemplateError, build_content, build_guide, fingerprint, select_template, validate_content, validate_profile, revision_diff
    from flow1c_templates_store import LibraryStore, atomic_bytes, atomic_json, documentation_root, exclusive, identifier, new_id, read_json, safe_path, sha256
    from flow1c_templates_extract import extract, source_format
except ModuleNotFoundError:
    from scripts.flow1c_templates_policy import TemplateError, build_content, build_guide, fingerprint, select_template, validate_content, validate_profile, revision_diff
    from scripts.flow1c_templates_store import LibraryStore, atomic_bytes, atomic_json, documentation_root, exclusive, identifier, new_id, read_json, safe_path, sha256
    from scripts.flow1c_templates_extract import extract, source_format

ACTIONS = ("list", "intake", "inspect", "profile-save", "activate", "history", "status", "resume", "rollback", "resolve", "configure", "relocate", "defer")
DOCUMENT_ACTIONS = ("document-plan", "document-write", "document-validate")


def validate_schema(root: Path, name: str, value: dict) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise TemplateError("TEMPLATE_DEPENDENCY_UNAVAILABLE", "jsonschema is unavailable.",
                            next_action="Bootstrap the template-markdown/template-docx capability and resume.") from exc
    schema = json.loads((root / "schemas" / (name + ".schema.json")).read_text(encoding="utf-8"))
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    if error:
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", f"Schema {name}: {error.json_path}: constraint {error.validator} failed; correct that property.")


class TemplateService:
    def __init__(self, workflow_root: Path, local: dict):
        self.workflow_root, self.local = workflow_root, local
        version = local.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or not 0 <= version <= 2:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Unsupported local configuration schema; install a compatible version.")
        self.documentation = documentation_root(str(local.get("documentation_path", "")), workflow_root)
        self.store = LibraryStore(self.documentation)
        config = json.loads((workflow_root / "config/document-types.json").read_text(encoding="utf-8"))
        if config.get("schema_version") != 1 or not isinstance(config.get("types"), list):
            raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Unsupported document type catalog.")
        self.types = {item["id"]: item for item in config["types"]}
        if len(self.types) != len(config["types"]):
            raise TemplateError("TEMPLATE_POLICY_CONFLICT", "Document type catalog contains duplicates.")
        configured = local.get("template_library", {}) or {}
        if configured.get("schema_version", 1) != 1:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Unsupported template_library configuration schema.")
        index = self.store.index()
        if index["library_id"]:
            validate_schema(workflow_root, "document-template-library", index)
        if configured.get("library_id") and index["library_id"] != configured["library_id"]:
            raise TemplateError("TEMPLATE_REVISION_STALE", "Configured library identity is missing or changed.")

    def result(self, **values: Any) -> dict:
        return {"schema_version": 1, "state": "READY", "ready": True, "operation_id": None,
                "library_id": self.store.index()["library_id"], "document_type": None,
                "template_id": None, "revision_id": None, "storage_kind": "documentation",
                "checks": [], "questions": [], "next_actions": [], "preserved_state": None, **values}

    def list(self) -> dict:
        index = self.store.index()
        variants = []
        for variant in index["templates"]:
            entry = {**variant, "registered_type": variant["document_type"] in self.types, "ready": False}
            if variant.get("active_revision"):
                try:
                    self.store.revision({"library_id": index["library_id"], "document_type": variant["document_type"],
                                         "template_id": variant["template_id"], "revision_id": variant["active_revision"]})
                    entry["ready"] = True
                except TemplateError as exc:
                    entry["error"] = exc.as_dict()
            variants.append(entry)
        return self.result(types=list(self.types.values()), templates=variants, contracts=self.contracts())

    def contracts(self) -> dict:
        """Expose authoritative schemas to clients whose arbitrary file reads are blocked."""
        return {name: json.loads((self.workflow_root / "schemas" / ("document-template-" + name + ".schema.json")).read_text(encoding="utf-8"))
                for name in ("profile", "plan-input")}

    def intake(self, request: dict) -> dict:
        operation_id = identifier(request.get("operation_id") or new_id())
        operation_root = self.store.operation_path(operation_id)
        checkpoint_file = operation_root / "checkpoint.json"
        intent = {k: request.get(k) for k in ("source", "document_type", "template_id", "variant_name", "parent_revision", "resources_root")}
        with exclusive(self.store.root):
            if checkpoint_file.exists():
                checkpoint = self.store.operation(operation_id)
                if checkpoint["intent_hash"] != fingerprint(intent):
                    raise TemplateError("TEMPLATE_REVISION_CONFLICT", "operation_id was already used for different input.")
                return checkpoint
            kind = str(request.get("document_type") or "")
            if kind not in self.types:
                raise TemplateError("TEMPLATE_TYPE_AMBIGUOUS", "Select a registered document type from content/user intent.",
                                    preserved_state={"types": list(self.types)})
            raw_source = Path(str(request.get("source") or ""))
            if not raw_source.is_absolute():
                raise TemplateError("TEMPLATE_SOURCE_REQUIRED", "Source path must be explicit and absolute.")
            source = safe_path(raw_source)
            format_name, maximum = source_format(source)
            if not source.is_absolute() or not source.is_file():
                raise TemplateError("TEMPLATE_SOURCE_REQUIRED", "Supply an accessible source file.")
            index = self.store.index(create=True, project_reference=self.local.get("project_reference"))
            template_id = request.get("template_id")
            if template_id:
                variant = next((v for v in index["templates"] if v["template_id"] == template_id and v["document_type"] == kind), None)
                if variant is None:
                    raise TemplateError("TEMPLATE_SOURCE_REQUIRED", "Variant does not exist for this type.")
                if "parent_revision" not in request or request["parent_revision"] != variant.get("active_revision"):
                    raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Supply the expected active parent_revision.", preserved_state=variant)
            else:
                template_id = new_id()
                variant = {"template_id": template_id, "document_type": kind,
                           "name": request.get("variant_name") or source.stem, "active_revision": None,
                           "default": False, "revisions": []}
                index["templates"].append(variant)
            revision_id = new_id()
            staged = operation_root / "candidate"
            destination = staged / "source" / ("template.md" if format_name == "markdown" else "template.docx")
            # Orphaned pre-checkpoint copies never replace a successful intake.
            if destination.exists():
                if sha256(destination) != sha256(source):
                    raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Interrupted intake contains different bytes; use a new operation_id.")
                source_hash = sha256(destination)
            else:
                source_hash = self.store.stable_copy(source, destination, maximum)
            atomic_json(self.store.root / "library.json", index)
            checkpoint = self.result(state="RECEIVED", ready=False, operation_id=operation_id,
                document_type=kind, template_id=template_id, revision_id=revision_id,
                source_format=format_name, source_sha256=source_hash, original_name=source.name,
                parent_revision=request.get("parent_revision"), intent_hash=fingerprint(intent),
                resources_root=request.get("resources_root"), original_parent=str(source.parent),
                next_actions=["resume"], preserved_state={"active_revision": variant["active_revision"]})
            self.store.checkpoint(checkpoint)
        return self.resume(operation_id)

    def resume(self, operation_id: str) -> dict:
        with exclusive(self.store.root):
            checkpoint = self.store.operation(operation_id)
            if checkpoint["state"] == "ACTIVATING":
                self.store.revision({k: checkpoint[k] for k in ("library_id", "template_id", "revision_id", "document_type")})
                index = self.store.index()
                variant = next(v for v in index["templates"] if v["template_id"] == checkpoint["template_id"])
                if variant["active_revision"] not in {checkpoint["activation_expected_revision"], checkpoint["revision_id"]}:
                    raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Another agent changed active during activation recovery.")
                variant["active_revision"] = checkpoint["revision_id"]
                if checkpoint.get("activation_default"):
                    for other in index["templates"]:
                        if other["document_type"] == variant["document_type"]:
                            other["default"] = other is variant
                atomic_json(self.store.root / "library.json", index)
                checkpoint.update(state="ACTIVE", ready=True, next_actions=[])
                self.store.checkpoint(checkpoint)
                return checkpoint
            if checkpoint["state"] == "PROFILE_COMMITTING":
                final = self.store.revision_path(checkpoint["template_id"], checkpoint["revision_id"])
                staged = self.store.operation_path(operation_id) / "candidate"
                if not final.exists() and staged.exists():
                    final.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(staged, final)
                self.store.revision({k: checkpoint[k] for k in ("library_id", "template_id", "revision_id", "document_type")})
                index = self.store.index()
                variant = next(v for v in index["templates"] if v["template_id"] == checkpoint["template_id"])
                if checkpoint["revision_id"] not in variant["revisions"]:
                    variant["revisions"].append(checkpoint["revision_id"])
                    atomic_json(self.store.root / "library.json", index)
                checkpoint.update(state="VALIDATED", ready=True, next_actions=["activate"])
                self.store.checkpoint(checkpoint)
                return checkpoint
            if checkpoint["state"] in {"ACTIVE", "VALIDATED", "PROFILE_DRAFT", "NEEDS_CLARIFICATION", "DEFERRED"}:
                return checkpoint
            staged = self.store.operation_path(operation_id) / "candidate"
            source = staged / "source" / ("template.md" if checkpoint["source_format"] == "markdown" else "template.docx")
            if sha256(source) != checkpoint["source_sha256"]:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Accepted source changed; active revision preserved.")
            try:
                parent_root, parent_manifest = None, None
                if checkpoint.get("parent_revision"):
                    parent_pin = {"library_id": checkpoint["library_id"], "document_type": checkpoint["document_type"],
                                  "template_id": checkpoint["template_id"], "revision_id": checkpoint["parent_revision"]}
                    parent_root, parent_manifest = self.store.revision(parent_pin)
                if parent_manifest and parent_manifest["source_sha256"] == checkpoint["source_sha256"]:
                    extraction = read_json(parent_root / "extraction.json")
                    if not checkpoint.get("resources_root"):
                        for relative, digest in parent_manifest["hashes"].items():
                            if relative.startswith("source/") and relative != parent_manifest["source"]:
                                copied = safe_path(staged / relative, staged)
                                if not copied.exists():
                                    self.store.stable_copy(parent_root / relative, copied, 10 * 1024 * 1024)
                else:
                    extraction = extract(source, checkpoint["source_format"])
                validate_schema(self.workflow_root, "document-template-extraction", extraction)
                missing = []
                for resource in extraction["resources"]:
                    relative = unquote(urlsplit(resource).path)
                    if Path(relative).is_absolute() or ".." in Path(relative).parts or ":" in relative:
                        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Use relative resources without parent traversal.")
                    target = safe_path(staged / "source" / relative, staged / "source")
                    if target.exists():
                        continue
                    if not checkpoint.get("resources_root"):
                        missing.append(resource)
                        continue
                    allowed = safe_path(Path(checkpoint["resources_root"]))
                    original = safe_path(Path(checkpoint["original_parent"]) / relative, allowed)
                    # Preserve safe relative links unchanged; parent escapes need explicit relocation support.
                    if not original.is_file():
                        missing.append(resource)
                    elif not target.exists():
                        self.store.stable_copy(original, target, 10 * 1024 * 1024)
                extraction["missing_resources"] = missing
                atomic_json(staged / "extraction.json", extraction)
                checkpoint.update(state="PROFILE_DRAFT", next_actions=["inspect", "profile-save"], errors=[])
                if missing:
                    checkpoint.update(state="NEEDS_CLARIFICATION", questions=[{"reason": "resources", "resources": missing}])
            except TemplateError as exc:
                checkpoint.update(state="FAILED_RECOVERABLE", ready=False, errors=[exc.as_dict()], next_actions=["resume"])
            self.store.checkpoint(checkpoint)
            return checkpoint

    def inspect(self, request: dict) -> dict:
        offset, limit = int(request.get("offset", 0)), int(request.get("limit", 20))
        text_offset, guide_offset = int(request.get("text_offset", 0)), int(request.get("guide_offset", 0))
        guide_limit, metadata_offset = int(request.get("guide_limit", 8000)), int(request.get("metadata_offset", 0))
        if offset < 0 or not 1 <= limit <= 50:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Context page requires offset >=0 and limit 1..50.")
        if min(text_offset, guide_offset, metadata_offset) < 0 or not 1 <= guide_limit <= 8000:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Text offsets must be >=0; guide_limit is 1..8000.")
        if request.get("operation_id"):
            checkpoint = self.store.operation(request["operation_id"])
            root = self.store.operation_path(request["operation_id"]) / "candidate"
        else:
            pin = request["pin"]
            root, _ = self.store.revision(pin)
            checkpoint = self.result(**pin)
        extraction = read_json(root / "extraction.json")
        page, chars, next_text_offset = [], 0, None
        for target in extraction["targets"][offset:offset + limit]:
            if len(target.get("text", "")) > 8000:
                if page:
                    break
                if text_offset >= len(target["text"]):
                    raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "text_offset must point inside the selected target.")
                fragment = target["text"][text_offset:text_offset + 8000]
                next_text_offset = text_offset + len(fragment) if text_offset + len(fragment) < len(target["text"]) else None
                page.append({**target, "text": fragment, "text_truncated": next_text_offset is not None, "text_offset": text_offset})
                break
            size = len(json.dumps(target, ensure_ascii=False))
            if size + chars > 10000:
                if not page:
                    preview = {**target, "text": target.get("text", "")[:8000], "text_truncated": True}
                    page.append(preview)
                break
            page.append(target)
            chars += size
        next_offset = offset if next_text_offset is not None else offset + len(page)
        guide = (root / "filling-guide.md").read_text(encoding="utf-8") if (root / "filling-guide.md").exists() else None
        metadata = json.dumps(extraction["metadata"], ensure_ascii=False)
        return {**checkpoint, "targets": page, "total_targets": len(extraction["targets"]),
                "contracts": self.contracts() if offset == text_offset == guide_offset == metadata_offset == 0 else None,
                "coverage": {"offset": offset, "next_offset": next_offset if next_offset < len(extraction["targets"]) else None,
                             "next_text_offset": next_text_offset},
                "guide": guide[guide_offset:guide_offset + guide_limit] if guide else None,
                "guide_next_offset": guide_offset + guide_limit if guide and len(guide) > guide_offset + guide_limit else None,
                "metadata": extraction["metadata"] if len(metadata) <= 4000 else None,
                "metadata_text": metadata[metadata_offset:metadata_offset + 4000] if len(metadata) > 4000 else None,
                "metadata_next_offset": metadata_offset + 4000 if len(metadata) > metadata_offset + 4000 else None,
                "source_sha256": extraction["source_sha256"]}

    def profile_save(self, request: dict) -> dict:
        operation_id = identifier(request["operation_id"])
        profile = request["profile"]
        validate_schema(self.workflow_root, "document-template-profile", profile)
        if self.store.operation(operation_id)["state"] == "PROFILE_COMMITTING":
            self.resume(operation_id)
        with exclusive(self.store.root):
            checkpoint = self.store.operation(operation_id)
            if checkpoint["state"] in {"ACTIVE", "VALIDATED"}:
                root = self.store.revision_path(checkpoint["template_id"], checkpoint["revision_id"])
                if read_json(root / "profile.json") != profile:
                    raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Revisions are immutable; intake the same source with a new operation for rule corrections.")
                return checkpoint
            staged = self.store.operation_path(operation_id) / "candidate"
            extraction = read_json(staged / "extraction.json")
            atomic_json(staged / "profile-draft.json", profile)
            policy = self.types.get(checkpoint["document_type"], {}).get("policy")
            canonical = None
            if policy == "functional-spec":
                try:
                    from flow1c_sections import load_catalog
                except ModuleNotFoundError:
                    from scripts.flow1c_sections import load_catalog
                canonical = [s["section_id"] for s in load_catalog(self.workflow_root / "standards/functional-specification-sections.md")["sections"]]
            try:
                if extraction.get("missing_resources"):
                    raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Supply the missing local resources via a new intake with resources_root.")
                validate_profile(profile, extraction, policy=policy, canonical_ids=canonical)
                atomic_json(staged / "profile.json", profile)
                atomic_bytes(staged / "filling-guide.md", build_guide(profile).encode("utf-8"))
                validation = {"schema_version": 1, "state": "VALIDATED", "integrity": "PASSED", "completeness": "PASSED",
                              "layout_state": "NOT_APPLICABLE" if checkpoint["source_format"] == "markdown" else "UNVERIFIED"}
                atomic_json(staged / "validation.json", validation)
                parent = checkpoint.get("parent_revision")
                old_profile = None
                if parent:
                    old_root = self.store.revision_path(checkpoint["template_id"], parent)
                    old_profile = read_json(old_root / "profile.json")
                old_extraction = read_json(old_root / "extraction.json") if parent else None
                report = {"source_changed": True, "profile_changed": old_profile != profile,
                          **revision_diff(old_extraction, extraction, old_profile, profile)}
                if parent:
                    old_manifest = read_json(old_root / "manifest.json")
                    report["source_changed"] = old_manifest["source_sha256"] != checkpoint["source_sha256"]
                atomic_bytes(staged / "change-report.md", ("# Revision changes\n\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                hashes = {p.relative_to(staged).as_posix(): sha256(p) for p in staged.rglob("*") if p.is_file() and p.name != "profile-draft.json"}
                manifest = {"schema_version": 1, "state": "VALIDATED", "template_id": checkpoint["template_id"],
                            "revision_id": checkpoint["revision_id"], "document_type": checkpoint["document_type"],
                            "source_format": checkpoint["source_format"], "source_sha256": checkpoint["source_sha256"],
                            "original_name": checkpoint["original_name"], "parent_revision": parent, "operation_id": operation_id,
                            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "extractor_version": 1, "profile_builder_version": 1,
                            "source": "source/template.md" if checkpoint["source_format"] == "markdown" else "source/template.docx", "hashes": hashes}
                validate_schema(self.workflow_root, "document-template-manifest", manifest)
                atomic_json(staged / "manifest.json", manifest)
                index = self.store.index()
                variant = next(v for v in index["templates"] if v["template_id"] == checkpoint["template_id"])
                # Repeated files/rules reuse their revision even with another operation_id.
                reused = next((r for r in variant["revisions"] if read_json(self.store.revision_path(variant["template_id"], r) / "manifest.json")["source_sha256"] == manifest["source_sha256"]
                               and {k: v for k, v in read_json(self.store.revision_path(variant["template_id"], r) / "manifest.json")["hashes"].items() if k.startswith("source/")} == {k: v for k, v in manifest["hashes"].items() if k.startswith("source/")}
                               and read_json(self.store.revision_path(variant["template_id"], r) / "profile.json") == profile), None)
                if reused:
                    checkpoint["revision_id"] = reused
                else:
                    final = self.store.revision_path(checkpoint["template_id"], checkpoint["revision_id"])
                    final.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint.update(state="PROFILE_COMMITTING", next_actions=["resume"])
                    self.store.checkpoint(checkpoint)
                    os.rename(staged, final)
                    variant["revisions"].append(checkpoint["revision_id"])
                    atomic_json(self.store.root / "library.json", index)
                checkpoint.update(state="VALIDATED", ready=True, errors=[], questions=[], checks=[validation], next_actions=["activate"])
            except TemplateError as exc:
                checkpoint.update(state="NEEDS_CLARIFICATION", ready=False, errors=[exc.as_dict()],
                                  questions=profile.get("questions", []), next_actions=["profile-save"])
            self.store.checkpoint(checkpoint)
            return checkpoint

    def activate(self, request: dict) -> dict:
        if request.get("operation_id") and self.store.operation(request["operation_id"])["state"] == "ACTIVATING":
            self.resume(request["operation_id"])
        with exclusive(self.store.root):
            checkpoint = self.store.operation(request["operation_id"]) if request.get("operation_id") else self.result(**request["pin"], operation_id=new_id())
            pin = {key: checkpoint[key] for key in ("library_id", "document_type", "template_id", "revision_id")}
            self.store.revision(pin)
            index = self.store.index()
            variant = next(v for v in index["templates"] if v["template_id"] == pin["template_id"])
            if "expected_revision" not in request:
                raise TemplateError("TEMPLATE_REVISION_CONFLICT", "An explicit expected_revision (including null) is required.")
            if variant["active_revision"] != request["expected_revision"]:
                if (checkpoint["state"] == "ACTIVE" and variant["active_revision"] == pin["revision_id"]
                        and checkpoint.get("activation_expected_revision") == request["expected_revision"]
                        and checkpoint.get("activation_default", False) == bool(request.get("default"))):
                    return checkpoint
                raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Active revision changed; refresh history before switching.", preserved_state=variant)
            variant["active_revision"] = pin["revision_id"]
            if request.get("default"):
                for other in index["templates"]:
                    if other["document_type"] == variant["document_type"]:
                        other["default"] = other is variant
            checkpoint.update(state="ACTIVATING", ready=False, activation_expected_revision=request["expected_revision"],
                              activation_default=bool(request.get("default")), next_actions=["resume"])
            self.store.checkpoint(checkpoint)
            atomic_json(self.store.root / "library.json", index)
            checkpoint.update(state="ACTIVE", ready=True, next_actions=[], activation_expected_revision=request["expected_revision"],
                              activation_default=bool(request.get("default")))
            if checkpoint.get("operation_id"):
                self.store.checkpoint(checkpoint)
            return checkpoint

    def resolve(self, request: dict, request_root: Path) -> dict:
        request_root = safe_path(request_root, self.documentation)
        pin_path = request_root / "template-pin.json"
        saved_pin = read_json(pin_path) if pin_path.exists() else None
        pin = select_template(self.store.index(), request["document_type"], template_id=request.get("template_id"),
                              revision_id=request.get("revision_id"), pin=saved_pin)
        if saved_pin and any(saved_pin.get(k) != pin[k] for k in pin):
            raise TemplateError("TEMPLATE_REVISION_STALE", "An existing request keeps its pin; start a new request/copy to adopt another revision.")
        _, manifest = self.store.revision(pin)
        pin.update(schema_version=1, source_sha256=manifest["source_sha256"], profile_sha256=manifest["hashes"]["profile.json"],
                   source_format=manifest["source_format"])
        atomic_json(pin_path, pin)
        return self.result(**{k: v for k, v in pin.items() if k != "schema_version"}, pin=pin)

    def document_plan(self, request: dict, request_root: Path) -> dict:
        request_root = safe_path(request_root, self.documentation)
        pin = read_json(request_root / "template-pin.json")
        revision, manifest = self.store.revision(pin)
        profile = read_json(revision / "profile.json")
        operations = request["operations"]
        validate_schema(self.workflow_root, "document-template-plan-input", {"schema_version": 1, "operations": operations})
        validate_content(profile, operations)
        extraction = read_json(revision / "extraction.json")
        targets = {t["id"]: t for t in extraction["targets"]}
        for operation in operations:
            t = targets[operation["target_id"]]
            expected_kind = "text" if t["kind"] in {"field", "paragraph"} else t["kind"]
            if operation["kind"] != expected_kind:
                raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Operation kind does not match target.")
        if profile.get("canonical_mapping"):
            try:
                from flow1c_sections_policy import validate_section_content
            except ModuleNotFoundError:
                from scripts.flow1c_sections_policy import validate_section_content
            values = {op["target_id"]: op["value"] for op in operations}
            for section, mapped in profile["canonical_mapping"].items():
                validate_section_content(section, "\n".join(str(values.get(i, "")) for i in mapped))
        suffix = ".md" if manifest["source_format"] == "markdown" else ".docx"
        name = request.get("output_name") or ("result-" + new_id() + suffix)
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(suffix) or ":" in name or "\\" in name:
            raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "output_name must be a filename in the selected format.")
        output = safe_path(request_root / name, request_root)
        if output.exists():
            raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Output already exists; select a new version filename.")
        plan_id = new_id()
        plan = {"schema_version": 1, "state": "READY", "plan_id": plan_id, "pin": pin,
                "operations": operations, "content_sha256": fingerprint(operations), "output_name": name,
                "document_status": "UNVERIFIED_DRAFT"}
        atomic_json(request_root / "document-plans" / (plan_id + ".json"), plan)
        return self.result(state="PLANNED", plan_id=plan_id, pin=pin, output=str(output))

    def document_write(self, request: dict, request_root: Path) -> dict:
        request_root = safe_path(request_root, self.documentation)
        plan_path = safe_path(request_root / "document-plans" / (identifier(request["plan_id"]) + ".json"), request_root)
        plan = read_json(plan_path)
        if plan["pin"] != read_json(request_root / "template-pin.json") or plan["content_sha256"] != fingerprint(plan["operations"]):
            raise TemplateError("TEMPLATE_REVISION_STALE", "Plan content or revision pin changed.")
        revision, manifest = self.store.revision(plan["pin"])
        output = safe_path(request_root / plan["output_name"], request_root)
        validation_path = request_root / (plan["plan_id"] + ".validation.json")
        if plan["state"] == "WRITTEN":
            return self.document_validate({"plan_id": plan["plan_id"]}, request_root)
        if plan["state"] == "WRITE_PENDING" and output.exists():
            pending = plan["pending_result"]
            if sha256(output) != pending["output_sha256"]:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Interrupted output changed; preserved without replacement.")
            extract(output, manifest["source_format"])
            atomic_json(validation_path, pending)
            plan.update(state="WRITTEN", result=pending)
            atomic_json(plan_path, plan)
            return self.document_validate({"plan_id": plan["plan_id"]}, request_root)
        profile = read_json(revision / "profile.json")
        validate_content(profile, plan["operations"])
        source = safe_path(revision / manifest["source"], revision)
        extraction = read_json(revision / "extraction.json")
        if manifest["source_format"] == "markdown":
            try:
                from flow1c_markdown import write_bytes
            except ModuleNotFoundError:
                from scripts.flow1c_markdown import write_bytes
        else:
            try:
                from flow1c_docx import fill_template as write_bytes
            except ModuleNotFoundError:
                from scripts.flow1c_docx import fill_template as write_bytes
        data = write_bytes(source, extraction, plan["operations"])
        content_path = request_root / ("content-" + plan["plan_id"] + ".md")
        content_data = build_content(profile, plan["operations"])
        request_root.mkdir(parents=True, exist_ok=True)
        with exclusive(request_root):
            if output.exists():
                raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Output exists; preserved without replacement.")
            if content_path.exists() and content_path.read_bytes() != content_data:
                raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Document content sidecar conflicts with preserved user data.")
            if not content_path.exists():
                atomic_bytes(content_path, content_data)
            for relative, expected in manifest["hashes"].items():
                if relative.startswith("source/") and relative != manifest["source"]:
                    resource = safe_path(request_root / relative.removeprefix("source/"), request_root)
                    if resource.exists() and sha256(resource) != expected:
                        raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Result resource would overwrite user data.")
                    if not resource.exists():
                        atomic_bytes(resource, (revision / relative).read_bytes())
            fd, temp_name = tempfile.mkstemp(prefix=".document-", suffix=output.suffix, dir=request_root)
            temporary = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Re-read before publication: a valid package is not visual QA.
                final_structure = extract(temporary, manifest["source_format"])
                check = {"schema_version": 1, "state": "WRITTEN", "ready": True, "plan_id": plan["plan_id"],
                         "output": str(output), "output_sha256": sha256(temporary), "pin": plan["pin"],
                         "operations_sha256": plan["content_sha256"],
                         "document_status": "UNVERIFIED_DRAFT", "integrity": "PASSED", "structure": "PASSED",
                         "completeness": "PASSED", "layout_state": "NOT_APPLICABLE" if manifest["source_format"] == "markdown" else "UNVERIFIED",
                         "content": str(content_path), "content_sha256": sha256(content_path),
                         "resource_hashes": {relative.removeprefix("source/"): digest for relative, digest in manifest["hashes"].items()
                                             if relative.startswith("source/") and relative != manifest["source"]},
                         "next_actions": [] if manifest["source_format"] == "markdown" else ["Review the DOCX pages with a locally approved renderer before claiming visual layout validation."]}
                validate_schema(self.workflow_root, "document-template-validation", check)
                plan.update(state="WRITE_PENDING", pending_result=check)
                atomic_json(plan_path, plan)
                os.link(temporary, output)  # atomic no-clobber publication on the same filesystem
                atomic_json(validation_path, check)
                plan.update(state="WRITTEN", result=check)
                atomic_json(plan_path, plan)
            finally:
                temporary.unlink(missing_ok=True)
        return check

    def document_validate(self, request: dict, request_root: Path) -> dict:
        root = safe_path(request_root, self.documentation)
        check = read_json(root / (identifier(request["plan_id"]) + ".validation.json"))
        plan = read_json(root / "document-plans" / (identifier(request["plan_id"]) + ".json"))
        if (plan["pin"] != check["pin"] or plan["plan_id"] != check["plan_id"]
                or fingerprint(plan["operations"]) != plan["content_sha256"]
                or check.get("operations_sha256", plan["content_sha256"]) != plan["content_sha256"]):
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Document plan changed after writing; preserve the result and create a new plan.")
        revision, _ = self.store.revision(check["pin"])
        output = safe_path(Path(check["output"]), root)
        if not output.is_file() or sha256(output) != check["output_sha256"]:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Result changed after validation; preserved without overwriting.")
        if check.get("content"):
            content = safe_path(Path(check["content"]), root)
            if not content.is_file() or sha256(content) != check["content_sha256"]:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Document content changed after validation.")
            if "operations_sha256" not in check:
                profile = read_json(revision / "profile.json")
                if content.read_bytes() != build_content(profile, plan["operations"]):
                    raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Legacy document plan differs from the written content.")
        elif "operations_sha256" not in check:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Legacy plan binding cannot be checked without its content sidecar; preserve the result and create a new plan.")
        for relative, expected in check.get("resource_hashes", {}).items():
            resource = safe_path(root / relative, root)
            if not resource.is_file() or sha256(resource) != expected:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "A result resource is missing or changed after validation.")
        return check

    def dispatch(self, action: str, request: dict, request_root: Path | None = None) -> dict:
        if action == "list":
            return self.list()
        if action == "intake":
            return self.intake(request)
        if action in {"status", "resume"}:
            return self.resume(request["operation_id"]) if action == "resume" else self.store.operation(request["operation_id"])
        if action == "inspect":
            return self.inspect(request)
        if action == "profile-save":
            return self.profile_save(request)
        if action in {"activate", "rollback"}:
            return self.activate(request)
        if action == "history":
            index = self.store.index()
            variant = next((v for v in index["templates"] if v["template_id"] == request["template_id"]), None)
            if variant is None:
                raise TemplateError("TEMPLATE_SOURCE_REQUIRED", "Variant does not exist.")
            return self.result(template_id=variant["template_id"], active_revision=variant["active_revision"],
                revisions=[read_json(self.store.revision_path(variant["template_id"], r) / "manifest.json") for r in variant["revisions"]])
        if action == "defer":
            operation_id = identifier(request.get("operation_id") or new_id())
            with exclusive(self.store.root):
                existing = self.store.operation(operation_id) if (self.store.operation_path(operation_id) / "checkpoint.json").exists() else {}
                result = {**self.result(), **existing, "state": "DEFERRED", "operation_id": operation_id, "ready": True,
                                     "document_type": request.get("document_type") or existing.get("document_type"),
                                     "reason": str(request.get("reason") or "user postponed template intake")}
                self.store.checkpoint(result)
                return result
        if action == "relocate":
            destination = documentation_root(request["documentation_path"], self.workflow_root)
            return self.result(**self.store.relocate(destination, request["operation_id"]))
        if action == "resolve":
            if request_root is None:
                raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "A request root is required for revision pins.")
            return self.resolve(request, request_root)
        if action in DOCUMENT_ACTIONS and request_root is not None:
            return getattr(self, action.replace("-", "_"))(request, request_root)
        raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Unknown action or missing request root.")


def readiness(workflow_root: Path, local: dict, format_name: str, operation: str) -> dict:
    checks, errors = [], []
    for module in ("jsonschema", "markdown_it") + (("docx",) if format_name == "docx" else ()):
        import importlib.util
        present = importlib.util.find_spec(module) is not None
        checks.append({"component": module, "state": "READY" if present else "MISSING"})
        if not present:
            errors.append({"code": "TEMPLATE_DEPENDENCY_UNAVAILABLE", "component": module,
                           "next_action": f"Approve bootstrap.ps1 -Profile template-{'docx' if format_name == 'docx' else 'markdown'} dependency plan."})
    try:
        service = TemplateService(workflow_root, local)
        checks.append({"component": "documentation_path", "state": "READY", "path": str(service.documentation)})
    except (TemplateError, OSError, ValueError) as exc:
        errors.append(exc.as_dict() if hasattr(exc, "as_dict") else {"code": "TEMPLATE_STORAGE_UNAVAILABLE", "message": str(exc)})
    return {"schema_version": 1, "state": "READY" if not errors else "BLOCKED", "ready": not errors,
            "operation": operation, "source_format": format_name, "checks": checks, "errors": errors,
            "required_capabilities": ["template-core", "template-markdown"] + (["template-docx"] if format_name == "docx" else []),
            "layout_state": "UNVERIFIED" if format_name == "docx" else "NOT_APPLICABLE"}
