"""Bounded artifact intake, extraction and provenance."""

from __future__ import annotations

import argparse
import datetime as dt
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from flow1c import context as runtime
from flow1c import storage as storage
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state

ALLOWED_ARTIFACT_EXTENSIONS = {
    ".docx",
    ".xlsx",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".xml",
    ".png",
    ".jpg",
    ".jpeg",
}
DENIED_ARTIFACT_EXTENSIONS = {
    ".exe",
    ".dll",
    ".ps1",
    ".bat",
    ".cmd",
    ".js",
    ".ts",
    ".py",
    ".zip",
    ".7z",
    ".rar",
}
MAX_INTAKE_FILES = 200
MAX_INTAKE_BYTES = 500 * 1024 * 1024
MAX_STORED_ARTIFACT_PATH = 240


def artifact_index_path(code: str | None, *, product_root: Path) -> Path | None:
    if not code:
        return None
    return work_items.work_item_root(code, product_root=product_root) / "input" / "artifacts.json"


def load_artifact_index(code: str | None, *, product_root: Path) -> dict[str, Any]:
    path = artifact_index_path(code, product_root=product_root)
    if path is None:
        artifacts: list[dict[str, Any]] = []
        confirmed_absent: set[str] = set()
        for manifest_path in sorted(
            (runtime.project_root(product_root=product_root) / "inbox").glob("*/intake.json")
        ):
            manifest = storage.read_json(manifest_path, {})
            if isinstance(manifest, dict) and manifest.get("code") is None:
                artifacts.extend(
                    (item for item in manifest.get("artifacts", []) if isinstance(item, dict))
                )
                confirmed_absent.update(
                    (str(value) for value in manifest.get("confirmed_absent", []))
                )
        return {
            "schema_version": 1,
            "artifacts": artifacts,
            "confirmed_absent": sorted(confirmed_absent),
        }
    value = storage.read_json(path, {})
    if not isinstance(value, dict):
        value = {}
    return {
        "schema_version": 1,
        "artifacts": list(value.get("artifacts", [])),
        "confirmed_absent": list(value.get("confirmed_absent", [])),
    }


def enumerate_intake_files(
    sources: Iterable[str], *, excluded_roots: Iterable[Path] = ()
) -> tuple[list[tuple[Path, Path]], list[str]]:
    files: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    excluded = tuple((path.resolve() for path in excluded_roots))

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(
            (resolved == root or storage.path_is_within(resolved, root) for root in excluded)
        )

    for raw in sources:
        source = Path(raw).expanduser().resolve()
        if not source.exists():
            raise WorkflowError(f"Artifact source does not exist: {source}")
        if is_excluded(source):
            skipped.append(str(source))
            continue
        if storage.is_reparse_or_symlink(source):
            raise WorkflowError(f"Symlinks and reparse points are not accepted: {source}")
        if source.is_file():
            candidates = [(source, Path(source.name))]
        else:
            candidates = []
            for candidate in sorted(source.rglob("*")):
                if is_excluded(candidate):
                    if candidate.is_file():
                        skipped.append(str(candidate))
                    continue
                if storage.is_reparse_or_symlink(candidate):
                    skipped.append(str(candidate))
                    continue
                if candidate.is_file():
                    candidates.append((candidate, candidate.relative_to(source)))
        for candidate, relative in candidates:
            if candidate.name.startswith("~$"):
                skipped.append(str(candidate))
                continue
            suffix = candidate.suffix.casefold()
            if suffix in DENIED_ARTIFACT_EXTENSIONS or suffix not in ALLOWED_ARTIFACT_EXTENSIONS:
                skipped.append(str(candidate))
                continue
            files.append((candidate, relative))
    return (files, skipped)


def paths_are_equal(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def free_intake_target(root: Path, digest: str, source: Path) -> Path:
    parent = root / "sources" / digest
    target = parent / source.name
    if len(str(target)) <= MAX_STORED_ARTIFACT_PATH:
        return target
    target = parent / f"artifact-{digest[:12]}{source.suffix.casefold()}"
    if len(str(target)) > MAX_STORED_ARTIFACT_PATH:
        raise WorkflowError(
            "The intake destination path is too long. Configure a shorter documentation_path."
        )
    return target


def intake_destination(
    code: str | None, category: str, intake_id: str, stages: dict[str, Any], *, product_root: Path
) -> Path:
    data_root = runtime.project_root(product_root=product_root)
    category_config = stages.get("artifact_categories", {}).get(category, {})
    destination = category_config.get("destination", "attachments")
    if not code or destination == "inbox":
        return data_root / "inbox" / intake_id / "originals"
    if destination == "meetings":
        return (
            work_items.work_item_root(code, product_root=product_root)
            / "input"
            / "meetings"
            / intake_id
        )
    return (
        work_items.work_item_root(code, product_root=product_root)
        / "input"
        / "attachments"
        / intake_id
    )


def write_derived_artifact(
    source: Path, relative: Path, code: str | None, intake_id: str, *, product_root: Path
) -> str | None:
    if not code or source.suffix.casefold() in {".xlsx", ".png", ".jpg", ".jpeg"}:
        return None
    derived_root = (
        work_items.work_item_root(code, product_root=product_root) / "input" / "derived" / intake_id
    )
    target = derived_root / relative.with_suffix(relative.suffix + ".md")
    try:
        if source.suffix.casefold() in {".txt", ".md", ".csv"}:
            converted = source.read_text(encoding="utf-8-sig", errors="replace")
        else:
            from markitdown import MarkItDown

            converted = MarkItDown().convert(str(source)).text_content
        storage.write_text(
            target,
            "<!-- Generated by Flow1C artifact-intake. Rebuild instead of editing. -->\n\n"
            + converted,
        )
        return str(target.relative_to(runtime.project_root(product_root=product_root))).replace(
            "\\", "/"
        )
    except (ImportError, OSError, AttributeError, ValueError):
        return None


def persist_intake_files(
    files: list[tuple[Path, Path]],
    skipped: list[str],
    *,
    code: str | None,
    category: str,
    received_via: str,
    source_record: dict[str, Any] | None = None,
    index: dict[str, Any] | None = None,
    product_root: Path,
) -> dict[str, Any]:
    """Store accepted files through the common inbox/work-item artifact path."""
    if len(files) > MAX_INTAKE_FILES:
        raise WorkflowError(f"Intake contains more than {MAX_INTAKE_FILES} files.")
    total_bytes = sum((path.stat().st_size for path, _ in files))
    if total_bytes > MAX_INTAKE_BYTES:
        raise WorkflowError(
            f"Intake contains {total_bytes} bytes; the limit is {MAX_INTAKE_BYTES}."
        )
    intake_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    stages = runtime.load_stages(product_root=product_root)
    if category not in stages.get("artifact_categories", {}):
        raise WorkflowError(f"Unknown artifact category: {category}")
    destination = intake_destination(code, category, intake_id, stages, product_root=product_root)
    index = index or load_artifact_index(code, product_root=product_root)
    known_hashes = {item.get("sha256") for item in index["artifacts"]}
    copied: list[dict[str, Any]] = []
    for source, relative in files:
        digest = storage.sha256(source)
        if digest in known_hashes:
            skipped.append(source.name)
            continue
        target = destination / relative
        if target.exists():
            target = target.with_name(f"{target.stem}-{digest[:8]}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        derived = write_derived_artifact(
            target, relative, code, intake_id, product_root=product_root
        )
        relative_target = str(
            target.relative_to(runtime.project_root(product_root=product_root))
        ).replace("\\", "/")
        record = {
            "category": category,
            "name": source.name,
            "relative_path": relative_target,
            "sha256": digest,
            "size": source.stat().st_size,
            "media_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "received_at": storage.utc_now(),
            "received_via": received_via,
            "derived_path": derived,
        }
        if source_record:
            record["source"] = source_record
        copied.append(record)
        index["artifacts"].append(record)
        known_hashes.add(digest)
    index["confirmed_absent"] = sorted(set(index["confirmed_absent"]))
    if code:
        storage.write_json(artifact_index_path(code, product_root=product_root), index)
    else:
        manifest_path = destination.parent / "intake.json"
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "intake_id": intake_id,
            "code": None,
            "created_at": storage.utc_now(),
            "artifacts": copied,
            "confirmed_absent": index["confirmed_absent"],
        }
        if source_record:
            manifest["source"] = source_record
        storage.write_json(manifest_path, manifest)
    return {
        "state": "ACCEPTED",
        "intake_id": intake_id,
        "code": code,
        "destination": str(destination),
        "copied": copied,
        "skipped": skipped,
        "confirmed_absent": index["confirmed_absent"],
    }


def intake_free_request(
    gate: dict[str, Any], args: argparse.Namespace, *, product_root: Path
) -> OperationResult:
    if getattr(args, "code", None):
        raise WorkflowError(
            "Use draft-promote to attach a free request to a user-selected work item"
        )
    root = gate_state.request_root(gate, product_root=product_root)
    promote_intake_id = str(getattr(args, "promote_intake_id", "") or "").strip()
    inbox_records: dict[str, dict[str, Any]] = {}
    if promote_intake_id:
        if getattr(args, "source", None):
            raise WorkflowError("Use either promote_intake_id or source paths in one intake call")
        if not re.fullmatch("[A-Za-z0-9-]{8,64}", promote_intake_id):
            raise WorkflowError("Invalid intake ID")
        inbox_root = runtime.project_root(product_root=product_root) / "inbox" / promote_intake_id
        manifest = storage.read_json(inbox_root / "intake.json")
        if not isinstance(manifest, dict) or manifest.get("code") is not None:
            raise WorkflowError("Inbox intake is unavailable for this request")
        sources = []
        for item in manifest.get("artifacts", []):
            if not isinstance(item, dict):
                continue
            source = runtime.project_root(product_root=product_root) / str(
                item.get("relative_path", "")
            )
            if (
                not storage.path_is_within(source, inbox_root / "originals")
                or storage.is_reparse_or_symlink(source)
                or (not source.is_file())
                or (storage.sha256(source) != item.get("sha256"))
            ):
                raise WorkflowError("Inbox artifact changed; fetch it again before intake")
            sources.append(str(source))
            inbox_records[str(source.resolve())] = item
        if not sources:
            raise WorkflowError("Inbox intake has no verified artifacts")
        args.source = sources
    files, skipped = enumerate_intake_files(
        getattr(args, "source", []) or [], excluded_roots=(root,)
    )
    if not files:
        raise WorkflowError(
            "No supported files found. The user can describe the task in chat instead."
        )
    if (
        len(files) > MAX_INTAKE_FILES
        or sum((p.stat().st_size for p, _ in files)) > MAX_INTAKE_BYTES
    ) and (not args.confirm_large):
        raise WorkflowError("Large intake requires explicit confirmation")
    artifacts = gate.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        gate["artifacts"] = artifacts
    copied = []
    for source, _ in files:
        digest = storage.sha256(source)
        existing = next(
            (item for item in artifacts if isinstance(item, dict) and item.get("sha256") == digest),
            None,
        )
        if existing is not None:
            if source.suffix.lower() in {".docx", ".pdf"} and (not existing.get("derived_path")):
                target = gate_state.safe_request_file(root, str(existing["path"]))
                try:
                    from markitdown import MarkItDown

                    derived = target.with_suffix(target.suffix + ".md")
                    storage.write_text(
                        derived,
                        "<!-- Generated from accepted source. -->\n"
                        + MarkItDown().convert(str(target)).text_content,
                    )
                    existing["derived_path"] = str(derived.relative_to(root)).replace("\\", "/")
                    existing["derived_sha256"] = storage.sha256(derived)
                    existing.pop("extraction_error", None)
                except (ImportError, OSError, ValueError, AttributeError) as exc:
                    existing["extraction_error"] = str(exc)
                existing["extraction_status"] = (
                    "ready" if existing.get("derived_path") else "failed"
                )
                copied.append(existing)
            else:
                skipped.append(str(source))
            continue
        target = free_intake_target(root, digest, source)
        if paths_are_equal(source, target):
            skipped.append(str(source))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        record = {
            "path": str(target.relative_to(root)).replace("\\", "/"),
            "source_path": str(source),
            "name": source.name,
            "sha256": digest,
            "category": args.category or "chat_material",
            "received_at": storage.utc_now(),
        }
        inbox_record = inbox_records.get(str(source.resolve()))
        if inbox_record:
            record["source"] = inbox_record.get("source")
            record["inbox_intake_id"] = promote_intake_id
            record["category"] = inbox_record.get("category", record["category"])
        if source.suffix.lower() in {".docx", ".pdf"}:
            try:
                from markitdown import MarkItDown

                derived = target.with_suffix(target.suffix + ".md")
                storage.write_text(
                    derived,
                    "<!-- Generated from accepted source. -->\n"
                    + MarkItDown().convert(str(target)).text_content,
                )
                record["derived_path"] = str(derived.relative_to(root)).replace("\\", "/")
                record["derived_sha256"] = storage.sha256(derived)
            except (ImportError, OSError, ValueError, AttributeError) as exc:
                record["extraction_error"] = str(exc)
            record["extraction_status"] = "ready" if record.get("derived_path") else "failed"
        artifacts.append(record)
        copied.append(record)
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    evidence["artifacts"] = artifacts
    storage.write_json(evidence_path, evidence)
    gate_state.save_request(gate, product_root=product_root)
    _value = {
        "state": "ACCEPTED",
        "copied": copied,
        "skipped": skipped,
        "next": "continue_same_gate",
    }
    return OperationResult(_value, 0)


def artifact_intake(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={
            "NEEDS_CODE",
            "NEEDS_INPUT",
            "NEEDS_CONFIRMATION",
            "READY",
            "READY_WITH_DEVIATIONS",
            "UNVERIFIED_DRAFT",
            "WAITING_USER",
            "BLOCKED",
        },
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_intake", product_root=product_root)
    if gate.get("mode", "formal") != "formal":
        return intake_free_request(gate, args, product_root=product_root)
    stages = runtime.load_stages(product_root=product_root)
    category = str(args.category)
    if category not in stages.get("artifact_categories", {}):
        raise WorkflowError(f"Unknown artifact category: {category}")
    code = (
        str(
            getattr(args, "task_reference", "")
            or getattr(args, "code", "")
            or gate.get("work_reference")
            or gate.get("code")
            or ""
        ).strip()
        or None
    )
    if code:
        work_items.load_manifest(code, product_root=product_root)
    index = load_artifact_index(code, product_root=product_root)
    promote_intake_id = str(getattr(args, "promote_intake_id", "") or "").strip()
    if promote_intake_id:
        if not code:
            raise WorkflowError("Inbox promotion requires a user-assigned task reference.")
        if not re.fullmatch("[A-Za-z0-9-]{8,64}", promote_intake_id):
            raise WorkflowError("Invalid intake ID for promotion.")
        inbox_root = runtime.project_root(product_root=product_root) / "inbox" / promote_intake_id
        inbox_manifest_path = inbox_root / "intake.json"
        inbox_manifest = storage.read_json(inbox_manifest_path)
        if not isinstance(inbox_manifest, dict) or inbox_manifest.get("code") is not None:
            raise WorkflowError(f"Inbox intake is unavailable for promotion: {promote_intake_id}")
        records = [item for item in inbox_manifest.get("artifacts", []) if isinstance(item, dict)]
        if not records:
            raise WorkflowError("Inbox intake contains no artifacts to promote.")
        categories = {str(item.get("category", "")) for item in records}
        if len(categories) != 1:
            raise WorkflowError(
                "An inbox intake with mixed categories cannot be promoted as one package."
            )
        original_root = inbox_root / "originals"
        destination = intake_destination(
            code, categories.pop(), promote_intake_id, stages, product_root=product_root
        )
        if destination.exists():
            raise WorkflowError(f"Promotion destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(original_root), str(destination))
        promoted: list[dict[str, Any]] = []
        for record in records:
            old_path = runtime.project_root(product_root=product_root) / str(
                record.get("relative_path", "")
            )
            relative = old_path.relative_to(original_root)
            target = destination / relative
            promoted_record = {
                **record,
                "relative_path": str(
                    target.relative_to(runtime.project_root(product_root=product_root))
                ).replace("\\", "/"),
                "received_via": "inbox-promotion",
                "promoted_at": storage.utc_now(),
                "derived_path": write_derived_artifact(
                    target, relative, code, promote_intake_id, product_root=product_root
                ),
            }
            if promoted_record.get("sha256") not in {
                item.get("sha256") for item in index["artifacts"]
            }:
                index["artifacts"].append(promoted_record)
            promoted.append(promoted_record)
        storage.write_json(artifact_index_path(code, product_root=product_root), index)
        inbox_manifest.update(
            {
                "state": "promoted",
                "code": code,
                "promoted_at": storage.utc_now(),
                "artifacts": promoted,
            }
        )
        storage.write_json(inbox_manifest_path, inbox_manifest)
        _value = {
            "state": "ACCEPTED",
            "intake_id": promote_intake_id,
            "code": code,
            "destination": str(destination),
            "copied": promoted,
            "skipped": [],
        }
        return OperationResult(_value, 0)
    for value in getattr(args, "confirm_absence", []) or []:
        if value not in stages.get("artifact_categories", {}):
            raise WorkflowError(f"Unknown artifact category: {value}")
        if value not in index["confirmed_absent"]:
            index["confirmed_absent"].append(value)
    files, skipped = enumerate_intake_files(getattr(args, "source", []) or [])
    if (
        (getattr(args, "source", []) or [])
        and (not files)
        and (not (getattr(args, "confirm_absence", []) or []))
    ):
        raise WorkflowError(
            "No acceptable artifacts were found. Executables, scripts, archives, Office temporary files, symlinks and unsupported formats are rejected."
        )
    total_bytes = sum((path.stat().st_size for path, _ in files))
    if (len(files) > MAX_INTAKE_FILES or total_bytes > MAX_INTAKE_BYTES) and (
        not args.confirm_large
    ):
        raise WorkflowError(
            f"Intake contains {len(files)} files and {total_bytes} bytes; explicit large-batch confirmation is required."
        )
    received_via = str(
        getattr(args, "received_via", "")
        or (
            "folder"
            if any((Path(raw).is_dir() for raw in getattr(args, "source", []) or []))
            else "file"
        )
    )
    result = persist_intake_files(
        files,
        skipped,
        code=code,
        category=category,
        received_via=received_via,
        index=index,
        product_root=product_root,
    )
    _value = result
    return OperationResult(_value, 0)
