"""Work-item paths, creation, manifests and registry reconciliation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import git_runtime as git_runtime
from flow1c import registry as registry_service
from flow1c import storage as storage
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from scripts import flow1c_policy as policy


def work_item_root(reference: str, *, for_create: bool = False, product_root: Path) -> Path:
    """Map an opaque reference to a safe directory and discover legacy locations."""
    try:
        exact = policy.validate_reference(reference, required=True)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    assert exact is not None
    parent = runtime.project_data_root(product_root=product_root) / "work-items"
    slug = policy.reference_slug(exact)
    direct = parent / slug
    if direct.exists():
        existing = storage.read_json(direct / "manifest.yaml", {})
        existing_reference = (
            existing.get("work_reference") or existing.get("task_reference") or existing.get("code")
        )
        if existing_reference == exact:
            return direct
        if for_create:
            suffix = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:8]
            return parent / policy.reference_slug(exact, suffix=suffix)
    for manifest_path in parent.glob("*/manifest.yaml"):
        manifest = storage.read_json(manifest_path, {})
        if exact in {
            manifest.get("work_reference"),
            manifest.get("task_reference"),
            manifest.get("code"),
        }:
            return manifest_path.parent
    return direct


def fs_start(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    reference = runtime.resolve_reference_args(args, product_root=product_root)
    requested_reference = reference["work_reference"]
    if not requested_reference:
        raise WorkflowError(
            "A user-supplied project or task reference is required; it is never generated automatically."
        )
    data_root = runtime.project_data_root(product_root=product_root)
    resolution = registry_service.resolve_registry_reference(
        str(requested_reference),
        str(getattr(args, "reference_kind", "auto") or "auto"),
        data_root,
        product_root=product_root,
    )
    if resolution["state"] == "resolved":
        code = str(resolution["code"])
        item = resolution["specification"]
        title = item.get("title") or args.title or code
        requirement_ids = resolution["requirements"]
        requirements = resolution["requirement_records"]
        reference = {**reference, "task_reference": code, "work_reference": code}
    elif resolution["state"] == "ambiguous":
        raise WorkflowError(
            f"Registry reference '{requested_reference}' is ambiguous ({resolution['reason']}): "
            + ", ".join(resolution["candidates"])
        )
    elif resolution["state"] == "invalid":
        raise WorkflowError(
            "The latest registry import is structurally invalid; fix it or use an explicit registry_bypass provisional flow"
        )
    else:
        code = str(requested_reference)
        title = args.title
        requirement_ids = registry_service.split_ids(args.requirements)
        requirements = storage.read_json(
            data_root / "registry" / "normalized" / "requirements.json", {}
        )
        if not title or not requirement_ids:
            raise WorkflowError(
                f"{requested_reference} cannot be resolved to a usable registry scope. Provide --title and --requirements explicitly or use registry_bypass."
            )
    missing = [
        requirement_id for requirement_id in requirement_ids if requirement_id not in requirements
    ]
    if missing:
        raise WorkflowError("Requirements absent from normalized registry: " + ", ".join(missing))
    target = work_item_root(code, for_create=True, product_root=product_root)
    branch = f"fs/{target.name}/specification"
    if args.create_branch:
        git_runtime.create_branch(branch, product_root=product_root)
    if target.exists():
        raise WorkflowError(f"Work item already exists: {target}")
    template = product_root / "templates" / "work-item"
    if template.is_dir():
        shutil.copytree(template, target)
    else:
        for relative in ("input/attachments", "analysis", "specification", "reviews", "evidence"):
            (target / relative).mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "code": code,
        **reference,
        "work_item_slug": target.name,
        "title": title,
        "status": "clarification",
        "traceability_mode": "registry",
        "requirements_source": "registry",
        "requirements": requirement_ids,
        "requested_reference": str(requested_reference),
        "reference_resolution": resolution.get("reference_kind", "explicit"),
        "branches": {"documentation": branch, "extension": f"feature/{target.name}"},
        "approvals": {"functional_architect": "pending", "technical_architect": "pending"},
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    storage.write_json(target / "manifest.yaml", manifest)
    snapshot = {requirement_id: requirements[requirement_id] for requirement_id in requirement_ids}
    storage.write_json(target / "input" / "requirements.snapshot.yaml", snapshot)
    snapshot_path = target / "input" / "requirements.snapshot.yaml"
    manifest["registry"] = {
        "status": "verified",
        "snapshot_sha256": storage.sha256(snapshot_path),
        "source_status": resolution.get("source_status", "verified"),
        "source_sha256": resolution.get("source_sha256"),
        "scope": {"specification": code, "requirements": requirement_ids},
    }
    manifest["requirement_basis"] = {
        "type": "registry_snapshot",
        "path": "input/requirements.snapshot.yaml",
        "sha256": storage.sha256(snapshot_path),
    }
    storage.write_json(target / "manifest.yaml", manifest)
    replacements = {"{{WORK_REFERENCE}}": code, "{{FS_CODE}}": code, "{{FS_TITLE}}": str(title)}
    for path in target.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    _value = {"path": str(target), "requirements": requirement_ids, "branch": branch}
    return OperationResult(_value, 0)


def create_provisional_work_item(
    reference: dict[str, Any],
    *,
    title: str,
    user_brief: str,
    deviation: dict[str, Any],
    requirements: list[str] | None = None,
    product_root: Path,
) -> Path:
    """Create a full work item from user material without inventing any identifiers."""
    code = reference.get("work_reference")
    if not code:
        raise WorkflowError(
            "A user-supplied task_reference or project_reference is required for provisional work"
        )
    brief = str(user_brief or "").strip()
    if not brief:
        raise WorkflowError("A nonempty user brief is required for provisional work")
    target = work_item_root(str(code), for_create=True, product_root=product_root)
    if target.exists():
        raise WorkflowError(f"Work item already exists: {target}")
    template = product_root / "templates" / "work-item"
    if template.is_dir():
        shutil.copytree(template, target)
    else:
        for relative in ("input/attachments", "analysis", "specification", "reviews", "evidence"):
            (target / relative).mkdir(parents=True, exist_ok=True)
    (target / "evidence").mkdir(parents=True, exist_ok=True)
    (target / "input" / "attachments").mkdir(parents=True, exist_ok=True)
    brief_path = target / "input" / "user-brief.md"
    storage.write_text(brief_path, brief)
    artifacts_path = target / "input" / "artifacts.json"
    if not artifacts_path.exists():
        storage.write_json(artifacts_path, {"artifacts": [], "confirmed_absent": []})
    confirmed_at = str(deviation.get("recorded_at") or storage.utc_now())
    bypass = {
        "type": "registry_bypass",
        "reason": str(deviation.get("reason") or "Registry traceability was explicitly bypassed"),
        "actor": str(deviation.get("actor") or "user"),
        "user_statement": str(
            deviation.get("user_statement") or deviation.get("reason") or ""
        ).strip(),
        "confirmed_at": confirmed_at,
        "scope": str(deviation.get("scope") or "work-item"),
        "condition_ids": list(
            dict.fromkeys(
                (
                    str(item)
                    for item in deviation.get(
                        "condition_ids", deviation.get("waived_conditions", [])
                    )
                )
            )
        ),
        "waived_conditions": list(
            dict.fromkeys(
                (
                    str(item)
                    for item in deviation.get(
                        "waived_conditions", deviation.get("condition_ids", [])
                    )
                )
            )
        ),
        "unconfirmed_conditions": list(
            dict.fromkeys(
                [
                    "registry requirements",
                    "registry assignment",
                    "registry snapshot",
                    *deviation.get("waived_conditions", []),
                ]
            )
        ),
    }
    branch = f"fs/{target.name}/specification"
    now = storage.utc_now()
    manifest = {
        "schema_version": 1,
        "code": code,
        **reference,
        "work_item_slug": target.name,
        "title": str(title or code),
        "status": "clarification",
        "traceability_mode": "provisional",
        "requirements_source": "user_brief",
        "requirements": list(requirements or []),
        "registry": {"status": "bypassed", "snapshot_sha256": None, "bypass": bypass},
        "requirement_basis": {
            "type": "user_brief",
            "path": "input/user-brief.md",
            "sha256": storage.sha256(brief_path),
        },
        "branches": {"documentation": branch, "extension": f"feature/{target.name}"},
        "approvals": {"functional_architect": "pending", "technical_architect": "pending"},
        "created_at": now,
        "updated_at": now,
    }
    storage.write_json(target / "manifest.yaml", manifest)
    replacements = {
        "{{WORK_REFERENCE}}": str(code),
        "{{FS_CODE}}": str(code),
        "{{FS_TITLE}}": str(title or code),
    }
    for path in target.rglob("*.md"):
        if path == brief_path:
            continue
        text = path.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    return target


def load_manifest(code: str, *, product_root: Path) -> tuple[Path, dict[str, Any]]:
    try:
        code = policy.validate_reference(code, required=True) or ""
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    path = work_item_root(code, product_root=product_root) / "manifest.yaml"
    manifest = storage.read_json(path)
    if manifest is None:
        raise WorkflowError(f"Work item not found: {code}")
    return (path, manifest)


def reference_mismatch(code: str | None, requirements: list[str], *, product_root: Path) -> str:
    if not code:
        return ""
    try:
        policy.validate_reference(code, required=True)
    except ValueError as exc:
        return str(exc)
    manifest = storage.read_json(
        work_item_root(code, product_root=product_root) / "manifest.yaml", {}
    )
    registered = storage.read_json(
        runtime.project_data_root(product_root=product_root) / "registry/normalized/specifications.json",
        {},
    ).get(code, {})
    item = manifest or registered
    if not item:
        return f"{code} отсутствует среди рабочих элементов и в импортированном реестре."
    unexpected = sorted(set(requirements) - set(item.get("requirements", [])))
    return f"Требования {', '.join(unexpected)} не привязаны к {code}." if unexpected else ""


def reconcile_registry(
    code: str, source: str, mappings: dict[str, str] | None = None, *, product_root: Path
) -> tuple[int, dict[str, Any]]:
    """Validate a replacement registry and reconcile only explicit or exact identifiers."""
    manifest_path, manifest = load_manifest(code, product_root=product_root)
    if manifest.get("traceability_mode") != "provisional":
        raise WorkflowError("registry-reconcile requires a provisional work item")
    imported = registry_service.registry_import(
        argparse.Namespace(file=source, allow_errors=False), product_root=product_root
    )
    exit_code, import_result = (imported.exit_code, imported.value)
    if exit_code:
        return (exit_code, import_result)
    known = storage.read_json(
        runtime.project_data_root(product_root=product_root)
        / "registry"
        / "normalized"
        / "requirements.json",
        {},
    )
    proposed: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    supplied = mappings or {}
    candidate_ids = [str(value) for value in manifest.get("requirements", [])]
    candidate_ids.extend((str(value) for value in supplied))
    brief_path = work_item_root(code, product_root=product_root) / "input" / "user-brief.md"
    brief_text = (
        brief_path.read_text(encoding="utf-8-sig", errors="replace") if brief_path.is_file() else ""
    )
    candidate_ids.extend(re.findall("[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_.]*-\\d+", brief_text))
    for candidate in dict.fromkeys(candidate_ids):
        target = supplied.get(candidate, candidate)
        if target in known:
            proposed[candidate] = target
        else:
            conflicts.append(
                {
                    "source": candidate,
                    "proposed": supplied.get(candidate),
                    "reason": "no confirmed registry match",
                }
            )
    if not proposed:
        conflicts.append(
            {
                "source": "user_brief",
                "proposed": None,
                "reason": "explicit requirement mapping is required",
            }
        )
    item_root = work_item_root(code, product_root=product_root)
    report_path = item_root / "analysis" / "registry-reconcile-report.md"
    report_lines = [
        "# Registry reconciliation report",
        "",
        f"Work item: `{code}`",
        "",
        "## Proposed mappings",
        "",
    ]
    report_lines.extend(
        (f"- `{source_id}` → `{target_id}`" for source_id, target_id in proposed.items())
    )
    if not proposed:
        report_lines.append("No safe automatic matches were found.")
    report_lines.extend(["", "## Conflicts", ""])
    report_lines.extend((f"- `{item['source']}`: {item['reason']}" for item in conflicts))
    if not conflicts:
        report_lines.append("None.")
    storage.write_text(report_path, "\n".join(report_lines))
    if conflicts:
        return (
            1,
            {
                "state": "NEEDS_CONFIRMATION",
                "report_path": str(report_path),
                "proposed_mappings": proposed,
                "conflicts": conflicts,
            },
        )
    selected_ids = list(dict.fromkeys(proposed.values()))
    snapshot = {requirement_id: known[requirement_id] for requirement_id in selected_ids}
    snapshot_path = item_root / "input" / "requirements.snapshot.yaml"
    storage.write_json(snapshot_path, snapshot)
    mapping_path = item_root / "input" / "registry-mapping.json"
    storage.write_json(
        mapping_path, {"confirmed": True, "mapped_at": storage.utc_now(), "mappings": proposed}
    )
    manifest.update(
        traceability_mode="registry",
        requirements_source="registry",
        requirements=selected_ids,
        registry={"status": "verified", "snapshot_sha256": storage.sha256(snapshot_path)},
        requirement_basis={
            "type": "registry_snapshot",
            "path": "input/requirements.snapshot.yaml",
            "sha256": storage.sha256(snapshot_path),
        },
        updated_at=storage.utc_now(),
    )
    storage.write_json(manifest_path, manifest)
    for evidence_path in (item_root / "evidence").glob("*.json"):
        evidence = storage.read_json(evidence_path, {})
        if isinstance(evidence, dict):
            evidence["revalidation_required"] = True
            evidence["reconciled_at"] = storage.utc_now()
            storage.write_json(evidence_path, evidence)
    return (
        0,
        {
            "state": "RECONCILED",
            "report_path": str(report_path),
            "mapping_path": str(mapping_path),
            "requirements": selected_ids,
            "revalidation_required": True,
        },
    )


def registry_reconcile(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        mappings = json.loads(str(getattr(args, "mappings_json", "") or "{}"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Invalid mappings JSON: {exc}") from exc
    if not isinstance(mappings, dict):
        raise WorkflowError("mappings must be a JSON object")
    exit_code, result = reconcile_registry(
        str(args.code),
        str(args.file),
        {str(k): str(v) for k, v in mappings.items()},
        product_root=product_root,
    )
    _value = result
    return OperationResult(_value, exit_code)


def describe_created_work_item(created: dict[str, Any]) -> str:
    return f"Created {created['path']}\nRequirements: {', '.join(created['requirements'])}\nDocumentation branch: {created['branch']}"
